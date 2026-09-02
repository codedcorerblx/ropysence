"""
Real Gateway connection (wss://gateway.discord.gg): HELLO -> heartbeat loop
-> IDENTIFY or RESUME (Bearer token, intents=0) -> READY/RESUMED. On top of
that, a periodic loop calls back into presence_builder every `poll_interval`
seconds and pushes whatever activity it returns as a PRESENCE_UPDATE
(opcode 3).

run_gateway_with_reconnect() is the entry point: it wraps a single
connection attempt (_connect_and_run) in a retry loop with exponential
backoff + jitter, and tracks session_id/sequence across attempts so a
disconnect can RESUME instead of always doing a full re-IDENTIFY (faster
recovery, and Discord's documented preferred behavior for transient drops).

Everything else here reflects lessons learned the hard way against the real
Gateway (see git history / conversation): build_activity() does several
blocking HTTP calls, so it's dispatched via run_in_executor rather than
called directly, which would otherwise freeze heartbeats for its whole
duration and get the connection killed as a zombie. All ws.send() calls go
through a shared lock, since offloading the fetch means heartbeat sends and
presence-update sends can now genuinely race. On Ctrl+C, a final
"activities: []" PRESENCE_UPDATE is sent before the socket closes, so the
Roblox status actually clears from your profile instead of freezing --
and Ctrl+C always stops the retry loop too, never triggers a reconnect.
"""

import asyncio
import json
import random
import time

import websockets

from src.core.logging_setup import get_logger

log = get_logger("discord_gateway")

GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"

OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_PRESENCE_UPDATE = 3
OP_RESUME = 6
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11


class _SessionState:
    """Carries session_id/sequence across reconnect attempts so a dropped
    connection can RESUME instead of always doing a full re-IDENTIFY."""
    def __init__(self):
        self.session_id = None
        self.seq = None
        self.resumable = False


async def _connect_and_run(access_token: str, build_activity_fn, poll_interval: float,
                            status: str, alias: str, session: _SessionState) -> bool:
    """One connection attempt (open, IDENTIFY/RESUME, run until disconnect).
    Returns True if this attempt got at least as far as READY/RESUMED
    (used by the caller to decide whether to reset backoff). KeyboardInterrupt
    propagates untouched so the outer loop knows to stop, not reconnect."""
    log.info("connecting to Gateway (%s)", GATEWAY_URL)
    connected_ok = False
    try:
        ws = await websockets.connect(GATEWAY_URL)
    except Exception as e:
        log.error("failed to open Gateway WebSocket: %s", e)
        return False

    async with ws:
        log.info("Gateway WebSocket connection opened")

        try:
            raw_hello = await asyncio.wait_for(ws.recv(), timeout=15)
        except asyncio.TimeoutError:
            log.error("timed out waiting for HELLO from Gateway")
            return False

        hello = json.loads(raw_hello)
        if hello.get("op") != OP_HELLO:
            log.warning("expected HELLO (op=10), got op=%s -- continuing anyway", hello.get("op"))
        heartbeat_interval = hello["d"]["heartbeat_interval"] / 1000.0
        log.info("HELLO received, heartbeat interval=%.2fs", heartbeat_interval)

        last_ack = {"t": time.monotonic()}
        ready_event = asyncio.Event()
        send_lock = asyncio.Lock()

        async def safe_send(payload: str):
            # websockets does not support concurrent send() calls from
            # multiple coroutines on the same connection -- since the
            # presence fetch runs off-thread, its send() and heartbeat's
            # send() can genuinely race. Serialize them.
            async with send_lock:
                await ws.send(payload)

        async def heartbeat_loop():
            while True:
                await asyncio.sleep(heartbeat_interval)
                try:
                    await safe_send(json.dumps({"op": OP_HEARTBEAT, "d": session.seq}))
                    log.debug("heartbeat sent (seq=%s)", session.seq)
                except websockets.ConnectionClosed:
                    return
                if time.monotonic() - last_ack["t"] > heartbeat_interval * 2:
                    log.warning("no heartbeat ACK received recently -- connection may be stale")

        async def presence_loop():
            await ready_event.wait()
            log.info("presence polling loop started (interval=%ss)", poll_interval)
            loop = asyncio.get_event_loop()
            while True:
                try:
                    # build_activity_fn() does several sequential blocking
                    # HTTP calls. Running that directly on the event loop
                    # would freeze heartbeats/frame processing for the whole
                    # duration -- easily multiple seconds -- risking the
                    # connection getting killed as a zombie. Offload it.
                    activity = await loop.run_in_executor(None, build_activity_fn)
                except Exception as e:
                    log.error("build_activity_fn raised an exception, skipping this cycle: %s", e)
                    activity = None

                if activity:
                    try:
                        payload = json.dumps({
                            "op": OP_PRESENCE_UPDATE,
                            "d": {"since": 0, "activities": [activity], "status": status, "afk": False},
                        })
                    except (TypeError, ValueError) as e:
                        log.error("activity dict was not JSON-serializable, skipping this cycle: %s -- activity was: %r", e, activity)
                        await asyncio.sleep(poll_interval)
                        continue

                    try:
                        await safe_send(payload)
                        log.info("PRESENCE_UPDATE sent")
                    except websockets.ConnectionClosed:
                        log.error("failed to send PRESENCE_UPDATE, connection closed")
                        return
                    except Exception as e:
                        log.error("failed to send PRESENCE_UPDATE: %s", e)
                else:
                    log.debug("no activity produced this cycle, nothing sent")

                await asyncio.sleep(poll_interval)

        hb_task = asyncio.create_task(heartbeat_loop())
        presence_task = asyncio.create_task(presence_loop())
        log.debug("heartbeat and presence loop tasks started")

        if session.resumable and session.session_id and session.seq is not None:
            log.info("attempting RESUME (session_id=%s..., seq=%s)", session.session_id[:8], session.seq)
            try:
                await safe_send(json.dumps({
                    "op": OP_RESUME,
                    "d": {"token": f"Bearer {access_token}", "session_id": session.session_id, "seq": session.seq},
                }))
            except Exception as e:
                log.error("failed to send RESUME: %s", e)
                hb_task.cancel()
                presence_task.cancel()
                return False
        else:
            log.info("sending IDENTIFY (intents=0, scoped bearer token)")
            try:
                await safe_send(json.dumps({
                    "op": OP_IDENTIFY,
                    "d": {
                        "token": f"Bearer {access_token}",
                        "intents": 0,
                        "properties": {"os": "linux", "browser": alias, "device": alias},
                    },
                }))
            except Exception as e:
                log.error("failed to send IDENTIFY: %s", e)
                hb_task.cancel()
                presence_task.cancel()
                return False

        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    log.warning("received non-JSON frame, ignoring: %s", raw[:200])
                    continue

                op, t, d = msg.get("op"), msg.get("t"), msg.get("d")
                if msg.get("s") is not None:
                    session.seq = msg["s"]
                log.debug("frame received op=%s t=%s seq=%s", op, t, msg.get("s"))

                if op == OP_DISPATCH and t == "READY":
                    session.session_id = d.get("session_id", "")
                    session.resumable = True
                    connected_ok = True
                    log.info("READY received -- session_id=%s...", session.session_id[:8])
                    ready_event.set()

                elif op == OP_DISPATCH and t == "RESUMED":
                    connected_ok = True
                    log.info("RESUMED -- picked back up where we left off, no re-IDENTIFY needed")
                    ready_event.set()

                elif op == OP_DISPATCH:
                    log.debug("unhandled dispatch event t=%s (ignored, intents=0)", t)

                elif op == OP_HEARTBEAT_ACK:
                    last_ack["t"] = time.monotonic()
                    log.debug("heartbeat ACK received")

                elif op == OP_HEARTBEAT:
                    log.debug("server requested immediate heartbeat")
                    await safe_send(json.dumps({"op": OP_HEARTBEAT, "d": session.seq}))

                elif op == OP_INVALID_SESSION:
                    session.resumable = bool(d)
                    log.warning(
                        "INVALID_SESSION received (resumable=%s) -- will %s on reconnect",
                        session.resumable, "attempt RESUME" if session.resumable else "do a full IDENTIFY",
                    )
                    break

                elif op == OP_RECONNECT:
                    session.resumable = True
                    log.warning("server sent RECONNECT (op=7) -- reconnecting, will attempt RESUME")
                    break

                else:
                    log.warning("unrecognized opcode=%s received, ignoring", op)

        except KeyboardInterrupt:
            # Send an explicit "no activity" update before the connection
            # closes, so the Roblox status actually disappears from your
            # profile instead of freezing on its last value until Discord's
            # own session timeout eventually clears it.
            log.info("Ctrl+C received -- clearing Discord activity before shutting down")
            try:
                await safe_send(json.dumps({
                    "op": OP_PRESENCE_UPDATE,
                    "d": {"since": 0, "activities": [], "status": status, "afk": False},
                }))
                log.info("cleared PRESENCE_UPDATE sent (activities: []) -- status should disappear from your profile")
                await asyncio.sleep(0.3)  # brief grace period so the frame reaches Discord before we close
            except Exception as e:
                log.warning("failed to send clearing PRESENCE_UPDATE before shutdown: %s", e)
            hb_task.cancel()
            presence_task.cancel()
            raise
        except websockets.ConnectionClosedOK:
            log.info("Gateway connection closed cleanly")
        except websockets.ConnectionClosedError as e:
            log.warning("Gateway connection closed with error: code=%s reason=%s -- will reconnect", e.code, e.reason)
            session.resumable = True  # an abnormal close is the classic resumable case
        except Exception as e:
            log.error("unexpected error in Gateway receive loop: %s -- will reconnect", e)
        finally:
            hb_task.cancel()
            presence_task.cancel()
            log.debug("heartbeat and presence tasks cancelled")

    return connected_ok


async def run_gateway_with_reconnect(get_access_token_fn, build_activity_fn, poll_interval: float,
                                      status: str, alias: str, reconnect_enabled: bool = True,
                                      base_delay: float = 5, max_delay: float = 300, max_attempts: int = 0):
    """get_access_token_fn: zero-arg callable returning a valid access token
    (re-fetched on every attempt so a mid-run token refresh is picked up
    automatically -- see discord/oauth.get_access_token, which is cheap to
    call repeatedly since it returns the cached token when still valid).
    max_attempts: 0 means retry forever."""
    session = _SessionState()
    attempt = 0
    delay = base_delay

    while True:
        access_token = get_access_token_fn()
        connected_ok = await _connect_and_run(access_token, build_activity_fn, poll_interval, status, alias, session)

        if connected_ok:
            if attempt > 0:
                log.info("reconnect succeeded after %d attempt(s)", attempt)
            attempt = 0
            delay = base_delay  # a real, working session this round -- don't carry over backoff from before

        if not reconnect_enabled:
            log.info("auto-reconnect is disabled (script.reconnect.enabled=false) -- exiting after this disconnect")
            return

        attempt += 1
        if max_attempts and attempt > max_attempts:
            log.error("reached max reconnect attempts (%d) -- giving up", max_attempts)
            return

        jitter = random.uniform(0, delay * 0.25)
        wait_time = min(delay + jitter, max_delay)
        attempts_label = f"/{max_attempts}" if max_attempts else ""
        log.warning("reconnecting in %.1fs (attempt %d%s)", wait_time, attempt, attempts_label)
        await asyncio.sleep(wait_time)
        delay = min(delay * 2, max_delay)
