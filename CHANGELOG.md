# Changelog

## 1.0.1

- Added `human.discord.webhook`: a separate, human-readable status webhook
  (going online/offline/studio/game) that fires once per real state change,
  distinct from the raw technical log webhook.
- `script.dev.discord.webhook` and `human.discord.webhook` now both accept
  multiple URLs (`["url1","url2"]`), round-robined and sent concurrently to
  spread load and avoid any single webhook's rate limit.
- Fixed `WebhookLogHandler` crashing on startup when a webhook was
  configured -- it was duck-typing `logging.Handler` instead of properly
  subclassing it, and was missing the `.level` attribute Python's logging
  internals access directly.
- Fixed a shutdown race in the webhook flush loop where `close()` could
  shut down the send thread pool before the final flush had actually
  submitted its work.

## 1.0.0

Initial public release.
