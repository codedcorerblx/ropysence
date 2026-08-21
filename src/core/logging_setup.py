"""
Logging setup. Two-phase on purpose:

  1. setup_logging() at import time -- gets logs working immediately (all
     levels, to stdout) before options.txt has even been read, so early
     startup/config errors are never silently swallowed.
  2. apply_options(options) -- called once options.txt is loaded, narrows
     the console handler to the configured levels (script.dev.debug/info/
     warn/error) and attaches a Discord webhook handler if configured.

IMPORTANT: never pass the raw Roblox cookie or full Discord tokens to log
calls. Use redact() below, or just log presence/absence instead of the value.
"""

import logging
import os
import sys

_CONFIGURED = False
_CONSOLE_HANDLER = None


class _LevelFilter(logging.Filter):
    """Only allow through the specific levels in `allowed` -- a plain
    minimum-level filter doesn't fit here since script.dev.* toggles are
    independent per-level switches, not a single threshold."""

    def __init__(self, allowed: set):
        super().__init__()
        self.allowed = allowed

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno in self.allowed


def setup_logging():
    global _CONFIGURED, _CONSOLE_HANDLER
    if _CONFIGURED:
        return
    logging.addLevelName(logging.DEBUG, "DBG")
    logging.addLevelName(logging.INFO, "INF")
    logging.addLevelName(logging.WARNING, "WRN")
    logging.addLevelName(logging.ERROR, "ERR")
    logging.addLevelName(logging.CRITICAL, "CRT")

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    _CONSOLE_HANDLER = logging.StreamHandler(stream=sys.stdout)
    _CONSOLE_HANDLER.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S",
    ))
    root.addHandler(_CONSOLE_HANDLER)
    _CONFIGURED = True


def apply_options(options: dict):
    """Narrow console output to the configured levels and attach a webhook
    handler if script.dev.discord.webhook is set. Call once, after
    core.options.load_options() succeeds."""
    allowed = set()
    if options.get("script.dev.debug"):
        allowed.add(logging.DEBUG)
    if options.get("script.dev.info"):
        allowed.add(logging.INFO)
    if options.get("script.dev.warn", True):
        allowed.add(logging.WARNING)
    if options.get("script.dev.error", True):
        allowed.add(logging.ERROR)
    allowed.add(logging.CRITICAL)  # never silence CRT

    _CONSOLE_HANDLER.addFilter(_LevelFilter(allowed))
    get_logger("logging_setup").debug(
        "console log levels narrowed to: %s",
        sorted(logging.getLevelName(lvl) for lvl in allowed),
    )

    webhook_url = options.get("script.dev.discord.webhook", "")
    if webhook_url:
        from src.core.webhook_logger import WebhookLogHandler
        handler = WebhookLogHandler(
            webhook_url=webhook_url,
            flush_interval=options.get("script.dev.discord.webhook.interval", 30),
            alias=options.get("script.dev.alias", "ropysence"),
        )
        handler.addFilter(_LevelFilter(allowed))
        handler.setFormatter(logging.Formatter("[%(levelname)s] %(name)s: %(message)s"))
        logging.getLogger().addHandler(handler)
        get_logger("logging_setup").info("Discord webhook logging enabled (flush every %ss)", options.get("script.dev.discord.webhook.interval", 30))


def get_logger(name: str) -> logging.Logger:
    setup_logging()
    return logging.getLogger(name)


def redact(value, keep: int = 0) -> str:
    """Never log secrets in full. keep>0 shows a short, non-sensitive prefix."""
    if not value:
        return "<empty>"
    if keep <= 0:
        return "***REDACTED***"
    return f"{value[:keep]}...***REDACTED***"
