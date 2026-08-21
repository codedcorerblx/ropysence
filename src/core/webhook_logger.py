"""
A logging.Handler that buffers formatted log lines and flushes them to a
Discord webhook in periodic batches (script.dev.discord.webhook.interval),
rather than one HTTP request per log line, which would both hammer the
webhook's rate limit and be wasteful for a script logging every few seconds.

Runs its flush loop on a plain daemon thread, independent of asyncio, so it
works regardless of whether the event loop is currently running, blocked,
or between cycles.
"""

import threading
import time

import requests

DISCORD_CONTENT_LIMIT = 1900  # stay comfortably under Discord's 2000-char message limit


class WebhookLogHandler:
    """Duck-types as a logging.Handler (has handle/emit/setFormatter/addFilter/
    setLevel) without subclassing logging.Handler directly, to keep its own
    background-thread lifecycle explicit and easy to reason about."""

    def __init__(self, webhook_url: str, flush_interval: float, alias: str):
        self.webhook_url = webhook_url
        self.flush_interval = max(5, flush_interval)
        self.alias = alias
        self._buffer = []
        self._lock = threading.Lock()
        self._formatter = None
        self._filters = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._flush_loop, daemon=True, name="webhook-log-flusher")
        self._thread.start()

    # -- logging.Handler-compatible surface --------------------------------
    def setFormatter(self, fmt):
        self._formatter = fmt

    def addFilter(self, filt):
        self._filters.append(filt)

    def setLevel(self, level):
        pass  # filtering is handled by the shared _LevelFilter instances

    def handle(self, record) -> bool:
        for f in self._filters:
            if not f.filter(record):
                return False
        self.emit(record)
        return True

    def emit(self, record):
        try:
            line = self._formatter.format(record) if self._formatter else record.getMessage()
        except Exception:
            line = record.getMessage()
        with self._lock:
            self._buffer.append(line)

    def close(self):
        self._stop.set()

    # -- flush loop -----------------------------------------------------------
    def _flush_loop(self):
        while not self._stop.wait(self.flush_interval):
            self._flush_once()
        self._flush_once()  # final flush on shutdown

    def _flush_once(self):
        with self._lock:
            if not self._buffer:
                return
            lines, self._buffer = self._buffer, []

        text = "\n".join(lines)
        chunks = [text[i:i + DISCORD_CONTENT_LIMIT] for i in range(0, len(text), DISCORD_CONTENT_LIMIT)] or [""]

        for chunk in chunks:
            try:
                requests.post(
                    self.webhook_url,
                    json={"username": self.alias, "content": f"```\n{chunk}\n```"},
                    timeout=10,
                )
            except requests.RequestException:
                # Deliberately not logging this failure through the logging
                # system -- that could recurse back into this same handler.
                pass
