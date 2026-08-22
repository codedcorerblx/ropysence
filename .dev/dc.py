#!/usr/bin/env python3
"""
Custom Discord Rich Presence via OAuth2 (PKCE) + the real Gateway.

This mirrors what Metrolist does on Android, just in Python:
  1. OAuth2 Authorization Code flow with PKCE, scope "openid sdk.social_layer_presence"
     -> user approves in their browser, no password or full account token ever touches this script.
  2. Exchange the code for a scoped access_token / refresh_token.
  3. Open a real WebSocket to wss://gateway.discord.gg (the same Gateway the desktop
     client uses) and IDENTIFY with `Bearer <access_token>` and intents=0.
     Because the token only carries the presence scope, Discord grants a presence-only
     session: no messages, no DMs, no guild data -- just the ability to push a
     PRESENCE_UPDATE (opcode 3) frame.
  4. Heartbeat forever, keep the custom activity alive.

Setup before running:
  1. Create an app at https://discord.com/developers/applications
  2. OAuth2 tab -> add redirect URI: http://127.0.0.1:8969/callback
  3. OAuth2 tab -> enable "Public Client" (needed for PKCE without a client secret)
  4. Put the Application/Client ID below (or export DISCORD_CLIENT_ID)
  5. pip install requests websockets --break-system-packages

Note: while your app is unverified, only accounts on your app's team can complete
the OAuth grant -- that's fine for testing on your own account.
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlencode, urlparse, parse_qs

import requests
import websockets

# ---------------------------------------------------------------------------
# Logging setup -- INF / WRN / ERR / DBG prefixes
# ---------------------------------------------------------------------------
logging.addLevelName(logging.DEBUG, "DBG")
logging.addLevelName(logging.INFO, "INF")
logging.addLevelName(logging.WARNING, "WRN")
logging.addLevelName(logging.ERROR, "ERR")
logging.addLevelName(logging.CRITICAL, "CRT")

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("discord_presence")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
CLIENT_ID = os.environ.get("938303388137971713", "1539867391985582160")
REDIRECT_PORT = 8969
REDIRECT_URI = f"http://127.0.0.1:{REDIRECT_PORT}/callback"
SCOPES = "openid sdk.social_layer_presence"

AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
TOKEN_URL = "https://discord.com/api/v10/oauth2/token"
GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"

# Gateway opcodes
OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_PRESENCE_UPDATE = 3
OP_RESUME = 6
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11

if CLIENT_ID == "YOUR_APPLICATION_ID_HERE":
    log.warning("DISCORD_CLIENT_ID not set -- using placeholder, authorize() will fail")


# ---------------------------------------------------------------------------
# 1. PKCE + local redirect capture
# ---------------------------------------------------------------------------
def make_pkce_pair():
    log.debug("generating PKCE verifier/challenge pair")
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    log.info("PKCE pair generated (verifier length=%d)", len(verifier))
    return verifier, challenge


def capture_redirect(expected_state: str) -> str:
    """Spin up a one-shot local server to catch the OAuth redirect and return `code`."""
    result = {}
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            qs = parse_qs(urlparse(self.path).query)
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            if "error" in qs:
                err = qs["error"][0]
                log.error("Discord returned OAuth error: %s", err)
                self.wfile.write(b"<h1>Authorization denied. You can close this tab.</h1>")
                result["error"] = err
            else:
                log.info("redirect received with authorization code")
                self.wfile.write(b"<h1>Authorized! You can close this tab.</h1>")
                result["code"] = qs.get("code", [None])[0]
                result["state"] = qs.get("state", [None])[0]
            done.set()

        def log_message(self, *args):
            pass  # silence default HTTP logging (we do our own above)

    log.debug("starting local redirect-capture server on 127.0.0.1:%d", REDIRECT_PORT)
    try:
        server = HTTPServer(("127.0.0.1", REDIRECT_PORT), Handler)
    except OSError as e:
        log.error("could not bind local server on port %d: %s", REDIRECT_PORT, e)
        raise

    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    log.info("waiting for browser redirect (timeout=120s)...")

    if not done.wait(timeout=120):
        log.error("timed out waiting for OAuth redirect -- did the browser open?")
        server.server_close()
        raise RuntimeError("Timed out waiting for authorization redirect")

    server.server_close()
    log.debug("local redirect server closed")

    if "error" in result:
        raise RuntimeError(f"Authorization failed: {result['error']}")

    if result.get("state") != expected_state:
        log.error(
            "OAuth state mismatch (expected=%s..., got=%s...) -- possible interception",
            expected_state[:8], str(result.get("state"))[:8],
        )
        raise RuntimeError("OAuth state mismatch -- possible interception, aborting")
    log.debug("state verified OK")

    if not result.get("code"):
        log.error("no authorization code present in redirect")
        raise RuntimeError("No authorization code received (timed out?)")

    return result["code"]


def authorize() -> dict:
    log.info("starting OAuth2 authorization flow")
    verifier, challenge = make_pkce_pair()
    state = secrets.token_urlsafe(16)
    log.debug("state token generated (%s...)", state[:8])

    params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "state": state,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
    }
    url = f"{AUTHORIZE_URL}?{urlencode(params)}"
    log.info("opening browser for Discord authorization (client_id=%s...)", str(CLIENT_ID)[:8])
    opened = webbrowser.open(url)
    if not opened:
        log.warning("webbrowser.open() reported failure -- open this URL manually:\n%s", url)

    code = capture_redirect(expected_state=state)
    log.debug("authorization code obtained (length=%d)", len(code))

    log.info("exchanging authorization code for tokens")
    try:
        resp = requests.post(
            TOKEN_URL,
            data={
                "client_id": CLIENT_ID,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "code_verifier": verifier,
            },
            timeout=15,
        )
    except requests.RequestException as e:
        log.error("network failure during token exchange: %s", e)
        raise

    if resp.status_code != 200:
        log.error(
            "token exchange failed (HTTP %d): %s",
            resp.status_code, resp.text[:300],
        )
        resp.raise_for_status()

    tokens = resp.json()
    log.info(
        "token exchange succeeded (scope=%s, expires_in=%ss, refresh_token=%s)",
        tokens.get("scope"), tokens.get("expires_in"),
        "present" if tokens.get("refresh_token") else "MISSING",
    )
    if not tokens.get("refresh_token"):
        log.warning("no refresh_token returned -- session won't survive token expiry without re-auth")
    return tokens


# ---------------------------------------------------------------------------
# 2. Gateway connection + custom presence
# ---------------------------------------------------------------------------
def build_activity() -> dict:
    """Customize this -- this is your Rich Presence card."""
    log.debug("building activity payload")
    now_ms = int(time.time() * 1000)
    activity = {
        "name": "Custom Presence",
        "type": 0,  # 0 Playing, 1 Streaming, 2 Listening, 3 Watching, 5 Competing
        "state": "via a Python script",
        "details": "Talking to the real Gateway",
        "timestamps": {"start": now_ms},
        "assets": {
            "large_image": "mp:external/https/example.com/cover.png",  # or an uploaded app asset key
            "large_text": "Hover text",
        },
        # Buttons only render for OTHER users viewing your profile, never for you.
        "buttons": ["Visit", "Repo"],
        # NOTE: with sdk.social_layer_presence, arbitrary runtime button URLs may be
        # silently dropped -- see WRN logged in run_presence() after sending.
    }
    log.debug("activity payload = %s", json.dumps(activity))
    return activity


async def run_presence(access_token: str):
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
        except Exception as e:
            log.error("failed to receive HELLO: %s", e)
            raise

        hello = json.loads(raw_hello)
        if hello.get("op") != OP_HELLO:
            log.warning("expected HELLO (op=10), got op=%s -- continuing anyway", hello.get("op"))

        heartbeat_interval = hello["d"]["heartbeat_interval"] / 1000.0
        log.info("HELLO received, heartbeat interval=%.2fs", heartbeat_interval)

        last_ack = {"t": time.monotonic()}

        async def heartbeat_loop():
            while True:
                await asyncio.sleep(heartbeat_interval)
                try:
                    await ws.send(json.dumps({"op": OP_HEARTBEAT, "d": None}))
                    log.debug("heartbeat sent")
                except websockets.ConnectionClosed as e:
                    log.error("heartbeat send failed, connection closed: %s", e)
                    return
                if time.monotonic() - last_ack["t"] > heartbeat_interval * 2:
                    log.warning("no heartbeat ACK received recently -- connection may be stale")

        hb_task = asyncio.create_task(heartbeat_loop())
        log.debug("heartbeat loop task started")

        log.info("sending IDENTIFY (intents=0, scoped bearer token)")
        try:
            await ws.send(json.dumps({
                "op": OP_IDENTIFY,
                "d": {
                    "token": f"Bearer {access_token}",
                    "intents": 0,
                    "properties": {
                        "os": "linux",
                        "browser": "python-presence-script",
                        "device": "python-presence-script",
                    },
                },
            }))
        except Exception as e:
            log.error("failed to send IDENTIFY: %s", e)
            hb_task.cancel()
            raise

        presence_sent = False
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
                    activity = build_activity()
                    try:
                        await ws.send(json.dumps({
                            "op": OP_PRESENCE_UPDATE,
                            "d": {
                                "since": 0,
                                "activities": [activity],
                                "status": "online",
                                "afk": False,
                            },
                        }))
                        presence_sent = True
                        log.info("PRESENCE_UPDATE sent -- check your Discord profile")
                        log.warning(
                            "if custom buttons/URLs don't appear, they may be silently "
                            "rejected under sdk.social_layer_presence -- configure static "
                            "Rich Presence buttons in the dev portal instead"
                        )
                    except Exception as e:
                        log.error("failed to send PRESENCE_UPDATE: %s", e)

                elif op == OP_DISPATCH:
                    log.debug("unhandled dispatch event t=%s (ignored, intents=0)", t)

                elif op == OP_HEARTBEAT_ACK:
                    last_ack["t"] = time.monotonic()
                    log.debug("heartbeat ACK received")

                elif op == OP_HEARTBEAT:
                    log.debug("server requested immediate heartbeat")
                    await ws.send(json.dumps({"op": OP_HEARTBEAT, "d": None}))

                elif op == OP_INVALID_SESSION:
                    resumable = bool(d)
                    log.error("INVALID_SESSION received (resumable=%s) -- rerun the script", resumable)
                    break

                elif op == OP_RECONNECT:
                    log.warning("server sent RECONNECT (op=7) -- closing to reconnect")
                    break

                else:
                    log.warning("unrecognized opcode=%s received, ignoring", op)

        except websockets.ConnectionClosedOK:
            log.info("Gateway connection closed cleanly")
        except websockets.ConnectionClosedError as e:
            log.error("Gateway connection closed with error: code=%s reason=%s", e.code, e.reason)
        except Exception as e:
            log.error("unexpected error in Gateway receive loop: %s", e)
        finally:
            hb_task.cancel()
            log.debug("heartbeat task cancelled")
            if not presence_sent:
                log.warning("connection ended before presence was ever sent")


def main():
    log.info("starting discord_presence script")
    try:
        tokens = authorize()
    except Exception as e:
        log.error("authorization failed, aborting: %s", e)
        sys.exit(1)

    log.info("authorization complete, moving to Gateway phase")
    try:
        asyncio.run(run_presence(tokens["access_token"]))
    except KeyboardInterrupt:
        log.info("interrupted by user, shutting down")
    except Exception as e:
        log.error("fatal error in presence loop: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
