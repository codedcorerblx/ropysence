#!/usr/bin/env python3
"""
Step 2 of the rebuild: same confirmed-working Discord OAuth/Gateway code as
before, same confirmed-working Roblox presence calls as before -- now wired
together in one file, deliberately still simple: no threading, no config
file, no encrypted storage, cookie/token typed fresh each run. Just the
integration point, isolated, so we can see if IT is what breaks.

Usage:
    pip install requests websockets --break-system-packages
    export DISCORD_CLIENT_ID=your_id_here
    python dc_rblx.py
"""

import asyncio
import base64
import getpass
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
log = logging.getLogger("dc_rblx")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
CLIENT_ID = os.environ.get("938303388137971713", "1539867391985582160")
REDIRECT_PORT = 8969
REDIRECT_URI = f"http://127.0.0.1:{REDIRECT_PORT}/callback"
SCOPES = "openid sdk.social_layer_presence"
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "5"))

AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
TOKEN_URL = "https://discord.com/api/v10/oauth2/token"
GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"

OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_PRESENCE_UPDATE = 3
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11

ROBLOX_AUTH_URL = "https://users.roblox.com/v1/users/authenticated"
ROBLOX_PRESENCE_URL = "https://presence.roblox.com/v1/presence/users"
ROBLOX_UNIVERSE_URL = "https://apis.roblox.com/universes/v1/places/{place_id}/universe"
ROBLOX_GAME_DETAILS_URL = "https://games.roblox.com/v1/games"
ROBLOX_ICON_URL = "https://thumbnails.roblox.com/v1/games/icons"
ROBLOX_HEADSHOT_URL = "https://thumbnails.roblox.com/v1/users/avatar-headshot"
ROBLOX_SERVERS_URL = "https://games.roblox.com/v1/games/{place_id}/servers/Public"

if CLIENT_ID == "YOUR_APPLICATION_ID_HERE":
    log.warning("DISCORD_CLIENT_ID not set -- using placeholder, authorize() will fail")


# ---------------------------------------------------------------------------
# 1. Discord: PKCE + local redirect capture + token exchange (unchanged)
# ---------------------------------------------------------------------------
def make_pkce_pair():
    log.debug("generating PKCE verifier/challenge pair")
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    log.info("PKCE pair generated (verifier length=%d)", len(verifier))
    return verifier, challenge


def capture_redirect(expected_state: str) -> str:
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
            pass

    log.debug("starting local redirect-capture server on 127.0.0.1:%d", REDIRECT_PORT)
    server = HTTPServer(("127.0.0.1", REDIRECT_PORT), Handler)
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
        log.error("OAuth state mismatch -- possible interception, aborting")
        raise RuntimeError("OAuth state mismatch")
    if not result.get("code"):
        log.error("no authorization code present in redirect")
        raise RuntimeError("No authorization code received")

    return result["code"]


def authorize() -> dict:
    log.info("starting OAuth2 authorization flow")
    verifier, challenge = make_pkce_pair()
    state = secrets.token_urlsafe(16)

    params = {
        "client_id": CLIENT_ID, "response_type": "code", "redirect_uri": REDIRECT_URI,
        "scope": SCOPES, "state": state, "code_challenge_method": "S256", "code_challenge": challenge,
    }
    url = f"{AUTHORIZE_URL}?{urlencode(params)}"
    log.info("opening browser for Discord authorization (client_id=%s...)", str(CLIENT_ID)[:8])
    opened = webbrowser.open(url)
    if not opened:
        log.warning("webbrowser.open() reported failure -- open this URL manually:\n%s", url)

    code = capture_redirect(expected_state=state)
    log.debug("authorization code obtained (length=%d)", len(code))

    log.info("exchanging authorization code for tokens")
    resp = requests.post(TOKEN_URL, data={
        "client_id": CLIENT_ID, "grant_type": "authorization_code", "code": code,
        "redirect_uri": REDIRECT_URI, "code_verifier": verifier,
    }, timeout=15)
    if resp.status_code != 200:
        log.error("token exchange failed (HTTP %d): %s", resp.status_code, resp.text[:300])
        resp.raise_for_status()

    tokens = resp.json()
    log.info("token exchange succeeded (scope=%s, expires_in=%ss)", tokens.get("scope"), tokens.get("expires_in"))
    return tokens


# ---------------------------------------------------------------------------
# 2. Roblox: same calls as roblox_check.py, no changes to the logic
# ---------------------------------------------------------------------------
def roblox_verify_cookie(session: requests.Session) -> dict:
    log.info("verifying Roblox cookie...")
    resp = session.get(ROBLOX_AUTH_URL, timeout=10)
    log.info("Roblox auth check -> HTTP %d", resp.status_code)
    log.debug("Roblox auth check body: %s", resp.text)
    resp.raise_for_status()
    return resp.json()


def roblox_get_presence(session: requests.Session, uid: int) -> dict:
    resp = session.post(ROBLOX_PRESENCE_URL, json={"userIds": [uid]}, timeout=10)
    log.debug("Roblox presence -> HTTP %d, body=%s", resp.status_code, resp.text)
    resp.raise_for_status()
    presences = resp.json().get("userPresences", [])
    if not presences:
        log.warning("Roblox presence response had no entries, treating as offline")
        return {"userPresenceType": 0}
    return presences[0]


def roblox_get_universe_id(session: requests.Session, place_id: int):
    resp = session.get(ROBLOX_UNIVERSE_URL.format(place_id=place_id), timeout=10)
    log.debug("universe lookup -> HTTP %d, body=%s", resp.status_code, resp.text)
    if resp.status_code != 200:
        return None
    return resp.json().get("universeId")


def roblox_get_game_details(session: requests.Session, universe_id: int) -> dict:
    resp = session.get(ROBLOX_GAME_DETAILS_URL, params={"universeIds": universe_id}, timeout=10)
    log.debug("game details -> HTTP %d, body=%s", resp.status_code, resp.text)
    if resp.status_code != 200:
        return {}
    data = resp.json().get("data", [])
    return data[0] if data else {}


def roblox_get_game_icon(session: requests.Session, universe_id: int):
    resp = session.get(ROBLOX_ICON_URL, params={
        "universeIds": universe_id, "size": "512x512", "format": "Png", "isCircular": "false",
    }, timeout=10)
    log.debug("game icon -> HTTP %d, body=%s", resp.status_code, resp.text[:300])
    if resp.status_code != 200:
        return None
    data = resp.json().get("data", [])
    return data[0]["imageUrl"] if data else None


def roblox_get_headshot(session: requests.Session, user_id: int):
    resp = session.get(ROBLOX_HEADSHOT_URL, params={
        "userIds": user_id, "size": "150x150", "format": "Png", "isCircular": "false",
    }, timeout=10)
    log.debug("headshot -> HTTP %d, body=%s", resp.status_code, resp.text[:300])
    if resp.status_code != 200:
        return None
    data = resp.json().get("data", [])
    return data[0]["imageUrl"] if data else None


def roblox_find_matching_server(session: requests.Session, place_id: int, game_id: str, max_pages: int = 5):
    cursor = ""
    for page in range(1, max_pages + 1):
        params = {"sortOrder": "Asc", "limit": 100}
        if cursor:
            params["cursor"] = cursor
        resp = session.get(ROBLOX_SERVERS_URL.format(place_id=place_id), params=params, timeout=10)
        log.debug("server list page %d -> HTTP %d", page, resp.status_code)
        if resp.status_code != 200:
            return None
        body = resp.json()
        for s in body.get("data", []):
            if s.get("id") == game_id:
                log.info("matched server on page %d -- %s/%s players", page, s.get("playing"), s.get("maxPlayers"))
                return s.get("playing"), s.get("maxPlayers")
        cursor = body.get("nextPageCursor")
        if not cursor:
            break
    log.warning("no matching public server found for gameId=%s within %d page(s)", str(game_id)[:8], max_pages)
    return None


# ---------------------------------------------------------------------------
# 3. The integration point: Roblox presence -> Discord activity dict
# ---------------------------------------------------------------------------
# CORRECTION from the previous attempt: raw https:// URLs do NOT just work,
# and "mp:external/https/domain/path" (the old community hand-rolled format)
# doesn't either. Per Discord's current docs, an external image must first
# be registered through the "Proxy Application Assets" endpoint, which
# returns a *server-signed* external_asset_path -- only THAT, prefixed with
# "mp:", is a valid activity image value. Example of what that endpoint
# actually returns:
#   {"url": "https://google.com/favicon.ico",
#    "external_asset_path": "external/OCZzr1eoglei1yFsfSMClt6B95EI9W-dOhq7fbnn5aY/https/google.com/favicon.ico"}
# -> usable image value: "mp:external/OCZzr1eoglei1yFsfSMClt6B95EI9W-dOhq7fbnn5aY/https/google.com/favicon.ico"
# The endpoint accepts at most 2 URLs per call, which conveniently matches
# our large_image + small_image.
DISCORD_EXTERNAL_ASSETS_URL = "https://discord.com/api/v10/applications/{client_id}/external-assets"

# For a fallback image when Roblox's own thumbnail can't be fetched: this
# proxy endpoint only works for reachable https:// URLs, not local files --
# so a local icon.png still can't be used directly here either. The one way
# to use a local icon.png as a fallback is to upload it once as a Rich
# Presence "Art Asset" in your Discord application's dev portal
# (Application -> Rich Presence -> Art Assets), then reference it by the
# *name* you gave it there -- app-asset keys are a separate, non-URL format
# and skip the proxy step entirely. Set PLACEHOLDER_ASSET_KEY to that name;
# leave it as None to just omit the image instead.
PLACEHOLDER_ASSET_KEY = None  # e.g. "icon" after uploading icon.png in the dev portal


def discord_proxy_image_urls(access_token: str, urls: list) -> dict:
    """POST up to 2 raw https:// URLs to Discord's external-assets endpoint.
    Returns {original_url: 'mp:<external_asset_path>'} for URLs that were
    successfully proxied; URLs that fail are simply absent from the result,
    so callers should treat a missing key as 'omit this image'."""
    urls = [u for u in urls if u][:2]
    if not urls:
        return {}

    log.debug("proxying %d image URL(s) through Discord external-assets: %s", len(urls), urls)
    try:
        resp = requests.post(
            DISCORD_EXTERNAL_ASSETS_URL.format(client_id=CLIENT_ID),
            headers={"Authorization": f"Bearer {access_token}"},
            json={"urls": urls},
            timeout=10,
        )
    except requests.RequestException as e:
        log.warning("network failure proxying image URLs, images will be omitted this cycle: %s", e)
        return {}

    log.debug("external-assets proxy -> HTTP %d, body=%s", resp.status_code, resp.text[:500])
    if resp.status_code != 200:
        log.warning(
            "Discord rejected the external-assets proxy request (HTTP %d) -- images will be "
            "omitted this cycle. Body: %s", resp.status_code, resp.text[:300],
        )
        return {}

    mapping = {}
    for item in resp.json():
        original = item.get("url")
        path = item.get("external_asset_path")
        if original and path:
            mapping[original] = "mp:" + path
        else:
            log.warning("proxy response missing expected fields for one URL: %s", item)
    return mapping


def build_activity(session: requests.Session, uid: int, username: str, state_tracker: dict, access_token: str) -> dict:
    log.debug("build_activity: fetching Roblox presence for uid=%s", uid)
    presence = roblox_get_presence(session, uid)
    ptype = presence.get("userPresenceType", 0)
    log.info("build_activity: presence type=%s", ptype)

    details = "Offline"
    state = ""
    large_image_url = None
    small_image_url = roblox_get_headshot(session, uid)
    state_url = None
    large_url = None
    small_url = None
    buttons = []
    place_id = None
    game_id = None

    if ptype == 0:
        details = "Offline"
    elif ptype in (1, 4):
        details = "Online"
    elif ptype == 3:
        details = "In Studio"
    elif ptype == 2:
        place_id = presence.get("placeId")
        game_id = presence.get("gameId")
        log.info("build_activity: in game, placeId=%s gameId=%s", place_id, game_id)

        universe_id = roblox_get_universe_id(session, place_id) if place_id else None
        game_details = roblox_get_game_details(session, universe_id) if universe_id else {}
        game_name = game_details.get("name", "a game")
        state = game_name
        large_image_url = roblox_get_game_icon(session, universe_id) if universe_id else None

        match = roblox_find_matching_server(session, place_id, game_id) if (place_id and game_id) else None
        if match:
            current, mx = match
            details = f"In Game ({current}/{mx})"
        else:
            details = f"In Game ({username})"

        profile_url = f"https://www.roblox.com/users/{uid}/profile"
        if place_id and game_id:
            join_url = f"roblox://placeId={place_id}&gameInstanceId={game_id}"
            buttons.append({"label": "Join Game", "url": join_url})
            large_url = join_url
        buttons.append({"label": f"{username}'s Profile", "url": profile_url})
        state_url = profile_url
        small_url = profile_url
    else:
        log.warning("unrecognized presence type=%s, treating as offline", ptype)

    # Resolve raw Roblox CDN URLs into Discord-usable "mp:..." references.
    proxied = discord_proxy_image_urls(access_token, [large_image_url, small_image_url])

    large_image = proxied.get(large_image_url)
    if large_image_url and not large_image:
        log.warning("large image proxy failed for %s -- falling back to placeholder/omitting", large_image_url)
    if not large_image and PLACEHOLDER_ASSET_KEY:
        large_image = PLACEHOLDER_ASSET_KEY
        log.debug("using placeholder asset '%s' for large image", PLACEHOLDER_ASSET_KEY)

    small_image = proxied.get(small_image_url)
    if small_image_url and not small_image:
        log.warning("small image (avatar) proxy failed for %s -- omitting", small_image_url)

    # Only reset the elapsed-time counter when the state actually changes
    # (offline<->online<->in a *different* game), not on every poll cycle.
    signature = (ptype, place_id, game_id)
    if state_tracker.get("signature") != signature:
        state_tracker["signature"] = signature
        state_tracker["start_ms"] = int(time.time() * 1000)
        log.info("presence state changed (%s) -- resetting elapsed-time counter", signature)
    else:
        log.debug("presence state unchanged -- keeping existing start timestamp")

    activity = {
        "name": "Roblox",
        "type": 0,
        "application_id": CLIENT_ID,
        "details": details,
        "state": state,
        "timestamps": {"start": state_tracker["start_ms"]},
        "assets": {},
    }
    if state_url:
        activity["state_url"] = state_url
    if large_image:
        activity["assets"]["large_image"] = large_image
        activity["assets"]["large_text"] = state
        if large_url:
            activity["assets"]["large_url"] = large_url
    if small_image:
        activity["assets"]["small_image"] = small_image
        activity["assets"]["small_text"] = username
        if small_url:
            activity["assets"]["small_url"] = small_url
    if buttons:
        activity["buttons"] = [b["label"] for b in buttons]
        activity["metadata"] = {"button_urls": [b["url"] for b in buttons]}

    log.info("build_activity: result details='%s' state='%s'", details, state)
    return activity


# ---------------------------------------------------------------------------
# 4. Gateway connection (same mechanics as before, presence now polls Roblox)
# ---------------------------------------------------------------------------
async def run_presence(access_token: str, roblox_session: requests.Session, roblox_uid: int, roblox_username: str):
    log.info("connecting to Gateway (%s)", GATEWAY_URL)
    ws = await websockets.connect(GATEWAY_URL)

    async with ws:
        log.info("Gateway WebSocket connection opened")

        raw_hello = await asyncio.wait_for(ws.recv(), timeout=15)
        hello = json.loads(raw_hello)
        heartbeat_interval = hello["d"]["heartbeat_interval"] / 1000.0
        log.info("HELLO received, heartbeat interval=%.2fs", heartbeat_interval)

        last_ack = {"t": time.monotonic()}
        ready_event = asyncio.Event()
        send_lock = asyncio.Lock()

        async def safe_send(payload: str):
            # websockets does not support concurrent send() calls from
            # multiple coroutines on the same connection -- now that
            # presence_loop's fetch runs off-thread, its send() and
            # heartbeat_loop's send() can genuinely race. Serialize them.
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
            log.info("presence polling loop started (interval=%ss)", POLL_INTERVAL)
            loop = asyncio.get_event_loop()
            state_tracker = {}  # persists across polls -- powers the timestamp fix
            while True:
                try:
                    # build_activity() does several sequential blocking HTTP
                    # calls (presence, headshot, universe, game details,
                    # icon, up to 5 pages of server matching). Running that
                    # directly on the event loop freezes heartbeats and
                    # frame processing for the whole duration -- easily
                    # multiple seconds -- which can get the connection
                    # killed as a zombie. Push it to a worker thread instead.
                    activity = await loop.run_in_executor(
                        None, build_activity, roblox_session, roblox_uid, roblox_username, state_tracker, access_token,
                    )
                    try:
                        payload = json.dumps({
                            "op": OP_PRESENCE_UPDATE,
                            "d": {"since": 0, "activities": [activity], "status": "online", "afk": False},
                        })
                    except (TypeError, ValueError) as e:
                        log.error("activity dict was not JSON-serializable, skipping this cycle: %s -- activity was: %r", e, activity)
                        await asyncio.sleep(POLL_INTERVAL)
                        continue

                    await safe_send(payload)
                    log.info("PRESENCE_UPDATE sent")
                except websockets.ConnectionClosed:
                    log.error("connection closed, stopping presence loop")
                    return
                except Exception as e:
                    log.error("presence cycle failed, will retry next interval: %s", e)
                await asyncio.sleep(POLL_INTERVAL)

        hb_task = asyncio.create_task(heartbeat_loop())
        presence_task = asyncio.create_task(presence_loop())

        log.info("sending IDENTIFY (intents=0, scoped bearer token)")
        await safe_send(json.dumps({
            "op": OP_IDENTIFY,
            "d": {
                "token": f"Bearer {access_token}",
                "intents": 0,
                "properties": {"os": "linux", "browser": "dc-rblx", "device": "dc-rblx"},
            },
        }))

        try:
            async for raw in ws:
                msg = json.loads(raw)
                op, t, d = msg.get("op"), msg.get("t"), msg.get("d")
                log.debug("frame received op=%s t=%s", op, t)

                if op == OP_DISPATCH and t == "READY":
                    log.info("READY received -- session_id=%s...", d.get("session_id", "")[:8])
                    ready_event.set()
                elif op == OP_HEARTBEAT_ACK:
                    last_ack["t"] = time.monotonic()
                elif op == OP_HEARTBEAT:
                    await safe_send(json.dumps({"op": OP_HEARTBEAT, "d": None}))
                elif op == OP_INVALID_SESSION:
                    log.error("INVALID_SESSION received -- rerun the script")
                    break
                elif op == OP_RECONNECT:
                    log.warning("server sent RECONNECT -- closing")
                    break
        except KeyboardInterrupt:
            # Send an explicit "no activity" PRESENCE_UPDATE before the
            # connection closes, so the Roblox status actually disappears
            # from your profile instead of freezing on its last value until
            # Discord's own session timeout eventually clears it.
            log.info("Ctrl+C received -- clearing Discord activity before shutting down")
            try:
                await safe_send(json.dumps({
                    "op": OP_PRESENCE_UPDATE,
                    "d": {"since": 0, "activities": [], "status": "online", "afk": False},
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
        finally:
            hb_task.cancel()
            presence_task.cancel()


def main():
    log.info("starting dc_rblx (step 2 integration test)")

    print("Paste your .ROBLOSECURITY cookie (input hidden):")
    cookie = getpass.getpass("Roblox Cookie: ").strip()
    if not cookie:
        log.error("no Roblox cookie entered, aborting")
        sys.exit(1)

    roblox_session = requests.Session()
    roblox_session.headers.update({"User-Agent": "Mozilla/5.0"})
    roblox_session.cookies.set(".ROBLOSECURITY", cookie, domain=".roblox.com")

    try:
        roblox_user = roblox_verify_cookie(roblox_session)
    except Exception as e:
        log.error("Roblox cookie verification failed, aborting: %s", e)
        sys.exit(1)

    roblox_uid = roblox_user["id"]
    roblox_username = roblox_user.get("name")
    log.info("Roblox authenticated as %s (uid=%s)", roblox_username, roblox_uid)

    try:
        tokens = authorize()
    except Exception as e:
        log.error("Discord authorization failed, aborting: %s", e)
        sys.exit(1)

    log.info("authorization complete, moving to Gateway phase")
    try:
        asyncio.run(run_presence(tokens["access_token"], roblox_session, roblox_uid, roblox_username))
    except KeyboardInterrupt:
        log.info("interrupted by user, shutting down")
    except Exception as e:
        log.error("fatal error: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
