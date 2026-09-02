# Changelog

## 1.2.0

- **Breaking change**: replaced the three-toggle button system
  (`rpc.button.join`/`.profile`/`.gamepage` + priority logic) with two
  fully generic slots: `rpc.button.one.{text,url}` and
  `rpc.button.two.{text,url}`. A button is shown only when both its text
  and url fully resolve (no missing placeholder) -- this is what makes the
  default Join button only appear while actually in a game, as an emergent
  property of its URL template rather than special-cased logic, and it
  applies equally to any custom reconfiguration of either slot.
- New placeholders: `{game.id}` (Roblox placeId) and `{game.instance}`
  (Roblox server/job id), plus `{user.id}`, all usable anywhere.
- New: unlimited user-defined custom placeholders --
  `placeholder.<name>="value"` in options.txt, referenced as
  `{custom.<name>}` anywhere. Values may reference other placeholders,
  including other custom ones, in any order (resolved with a few
  fixed-point passes so forward/backward references both work; circular
  references are detected and logged rather than hanging).
- If you have an existing `options.txt` from before this version, the old
  `rpc.button.*` keys are no longer read -- you'll want to add the new
  `rpc.button.one.*`/`rpc.button.two.*` keys manually (see README).

## 1.1.0

- Added Gateway auto-reconnect: on disconnect (network drop, Discord-issued
  `RECONNECT`, `INVALID_SESSION`, or any abnormal close), the connection is
  retried automatically with exponential backoff + jitter instead of the
  whole process exiting.
- Reconnects use Discord's `RESUME` protocol when the prior session is
  still valid (tracks `session_id` and sequence number across attempts),
  falling back to a full `IDENTIFY` only when required -- faster recovery
  than always re-authenticating from scratch.
- New config: `script.reconnect.enabled`, `.base_delay`, `.max_delay`,
  `.max_attempts`.
- Fixed a latent bug this surfaced: `PresenceBuilder` used to freeze the
  Discord access token at construction time. On a long-running process that
  reconnects (or just runs long enough for the ~7-day token to refresh),
  the image-proxy calls would keep using the stale token independently of
  whether the Gateway connection itself was fine. It now fetches the
  current token fresh on every use.
- Heartbeat frames now correctly include the last-seen sequence number
  (`d: seq` instead of always `d: null`), matching Discord's documented
  heartbeat format.

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
