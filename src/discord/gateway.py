"""
Real Gateway connection (wss://gateway.discord.gg): HELLO -> heartbeat loop
-> IDENTIFY (Bearer token, intents=0) -> READY. On top of that, a periodic
loop calls back into presence_builder every `poll_interval` seconds and
pushes whatever activity it returns as a PRESENCE_UPDATE (opcode 3).

Everything here reflects lessons learned the hard way against the real
Gateway (see git history / conversation): build_activity() does several
blocking HTTP calls, so it's dispatched via run_in_executor rather than
called directly, which would otherwise freeze heartbeats for its whole
duration and get the connection killed as a zombie. All ws.send() calls go
through a shared lock, since offloading the fetch means heartbeat sends and
presence-update sends can now genuinely race. On Ctrl+C, a final
"activities: []" PRESENCE_UPDATE is sent before the socket closes, so the
Roblox status actually clears from your profile instead of freezing.
"""

import asyncio
import json
import time

import websockets

from src.core.logging_setup import get_logger

log = get_logger("discord_gateway")

GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"

OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_PRESENCE_UPDATE = 3
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11


async def run_gateway(access_token: str, build_activity_fn, poll_interval: float, status: str, alias: str):
    """build_activity_fn: zero-arg callable returning a Discord activity dict
    or None (None = skip this cycle, e.g. rate limited)."""
    log.info("connecting to Gateway (%s)", GATEWAY_URL)
    try:
        ws = await websockets.connect(GATEWAY_URL)
    except Exception as e:
        log.error("failed to open Gateway WebSocket: %s", e)
        raise

    async with ws:
        log.info("Gateway WebSocket connection opened")

        try:
            raw_hello = await asyncio.wait_for(ws.recv(), timeout=15)
        except asyncio.TimeoutError:
            log.error("timed out waiting for HELLO from Gateway")
            raise

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
                    await safe_send(json.dumps({"op": OP_HEARTBEAT, "d": None}))
                    log.debug("heartbeat sent")
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
            raise

        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    log.warning("received non-JSON frame, ignoring: %s", raw[:200])
                    continue

                op, t, d = msg.get("op"), msg.get("t"), msg.get("d")
                log.debug("frame received op=%s t=%s", op, t)

                if op == OP_DISPATCH and t == "READY":
                    session_id = d.get("session_id", "")
                    log.info("READY received -- session_id=%s...", session_id[:8])
                    ready_event.set()

                elif op == OP_DISPATCH:
                    log.debug("unhandled dispatch event t=%s (ignored, intents=0)", t)

                elif op == OP_HEARTBEAT_ACK:
                    last_ack["t"] = time.monotonic()
                    log.debug("heartbeat ACK received")

                elif op == OP_HEARTBEAT:
                    log.debug("server requested immediate heartbeat")
                    await safe_send(json.dumps({"op": OP_HEARTBEAT, "d": None}))

                elif op == OP_INVALID_SESSION:
                    resumable = bool(d)
                    log.error("INVALID_SESSION received (resumable=%s) -- rerun the script", resumable)
                    break

                elif op == OP_RECONNECT:
                    log.warning("server sent RECONNECT (op=7) -- closing to reconnect")
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
            raise
        except websockets.ConnectionClosedOK:
            log.info("Gateway connection closed cleanly")
        except websockets.ConnectionClosedError as e:
            log.error("Gateway connection closed with error: code=%s reason=%s", e.code, e.reason)
        except Exception as e:
            log.error("unexpected error in Gateway receive loop: %s", e)
        finally:
            hb_task.cancel()
            presence_task.cancel()
            log.debug("heartbeat and presence tasks cancelled")
