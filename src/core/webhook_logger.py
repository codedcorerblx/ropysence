"""
A logging.Handler that buffers formatted log lines and flushes them to one
or more Discord webhooks in periodic batches (script.dev.discord.webhook.interval),
rather than one HTTP request per log line, which would both hammer the
webhook's rate limit and be wasteful for a script logging every few seconds.

Supports multiple webhook URLs: each chunk of a flush is sent to the next
URL in round-robin order, and chunks are dispatched concurrently rather
than one-after-another -- spreads load across endpoints (helps avoid any
single webhook's rate limit) and reduces total flush latency when there's
more than one chunk.

Runs its flush loop on a plain daemon thread, independent of asyncio, so it
works regardless of whether the event loop is currently running, blocked,
or between cycles.

Properly subclasses logging.Handler (rather than duck-typing handle/emit/
setFormatter/etc.) because Python's logging internals access attributes
like `.level` and `.filters` directly rather than through a method call --
Logger.callHandlers does `if record.levelno >= hdlr.level` before ever
calling hdlr.handle(), so a handler missing that attribute crashes the
whole logging call, not just webhook delivery. Subclassing gets all of
that correctly for free.
"""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait

import requests

DISCORD_CONTENT_LIMIT = 1900  # stay comfortably under Discord's 2000-char message limit


class WebhookLogHandler(logging.Handler):
    def __init__(self, webhook_urls: list, flush_interval: float, alias: str):
        super().__init__()
        self.webhook_urls = [u for u in (webhook_urls or []) if u]
        self.flush_interval = max(5, flush_interval)
        self.alias = alias
        self._buffer = []
        self._buffer_lock = threading.Lock()
        self._stop = threading.Event()
        self._rr_index = 0
        self._rr_lock = threading.Lock()
        self._pending_futures = []
        self._pool = (
            ThreadPoolExecutor(max_workers=min(4, len(self.webhook_urls)), thread_name_prefix="dev-webhook-send")
            if self.webhook_urls else None
        )
        self._thread = threading.Thread(target=self._flush_loop, daemon=True, name="webhook-log-flusher")
        self._thread.start()

    def emit(self, record: logging.LogRecord):
        try:
            line = self.format(record)
        except Exception:
            line = record.getMessage()
        with self._buffer_lock:
            self._buffer.append(line)

    def close(self):
        self._stop.set()
        # Let the background thread's OWN final _flush_once() actually run
        # and submit its futures before we touch the pool -- otherwise
        # close() can race ahead and shut the pool down mid-submission.
        self._thread.join(timeout=self.flush_interval + 2)
        if self._pool:
            wait(self._pending_futures, timeout=5)  # let the final flush actually land before exit
            self._pool.shutdown(wait=False)
        super().close()

    def _next_url(self) -> str:
        with self._rr_lock:
            url = self.webhook_urls[self._rr_index % len(self.webhook_urls)]
            self._rr_index += 1
        return url

    # -- flush loop -----------------------------------------------------------
    def _flush_loop(self):
        while not self._stop.wait(self.flush_interval):
            self._flush_once()
        self._flush_once()  # final flush on shutdown

    def _flush_once(self):
        if not self.webhook_urls:
            return
        with self._buffer_lock:
            if not self._buffer:
                return
            lines, self._buffer = self._buffer, []

        text = "\n".join(lines)
        chunks = [text[i:i + DISCORD_CONTENT_LIMIT] for i in range(0, len(text), DISCORD_CONTENT_LIMIT)] or [""]

        self._pending_futures = [self._pool.submit(self._send_chunk, chunk) for chunk in chunks]

    def _send_chunk(self, chunk: str):
        url = self._next_url()
        try:
            requests.post(
                url,
                json={"username": self.alias, "content": f"```\n{chunk}\n```"},
                timeout=10,
            )
        except requests.RequestException:
            # Deliberately not logging this failure through the logging
            # system -- that could recurse back into this same handler.
            pass
