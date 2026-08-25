"""
Sends clean, human-readable status update messages to one or more Discord
webhooks -- deliberately NOT the raw log firehose (see webhook_logger.py for
that). One short message per genuine presence state change, nothing else:
no batching backlog, no DBG/INF noise, no stack traces.

Multiple webhook URLs are supported and round-robined, same rationale as
the dev log webhook: spreads load across endpoints and avoids any single
one eating all the rate-limit budget. Sends happen on a small thread pool
so a slow/rate-limited webhook doesn't block the next notification.
"""

import threading
from concurrent.futures import ThreadPoolExecutor

import requests

from src.core.logging_setup import get_logger

log = get_logger("human_webhook")


class HumanWebhookNotifier:
    def __init__(self, webhook_urls: list, alias: str):
        self.webhook_urls = [u for u in (webhook_urls or []) if u]
        self.alias = alias
        self._rr_index = 0
        self._rr_lock = threading.Lock()
        self._pool = (
            ThreadPoolExecutor(max_workers=min(4, len(self.webhook_urls)), thread_name_prefix="human-webhook-send")
            if self.webhook_urls else None
        )
        if self.webhook_urls:
            log.info("human-readable webhook notifications enabled (%d URL(s))", len(self.webhook_urls))

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_urls)

    def notify(self, message: str):
        if not self.enabled or not message:
            return
        self._pool.submit(self._send, message)

    def _next_url(self) -> str:
        with self._rr_lock:
            url = self.webhook_urls[self._rr_index % len(self.webhook_urls)]
            self._rr_index += 1
        return url

    def _send(self, message: str):
        url = self._next_url()
        try:
            requests.post(url, json={"username": self.alias, "content": message}, timeout=10)
        except requests.RequestException as e:
            log.warning("failed to deliver human webhook notification: %s", e)

    def close(self):
        if self._pool:
            self._pool.shutdown(wait=False)
