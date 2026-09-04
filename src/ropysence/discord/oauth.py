"""
OAuth2 Authorization Code + PKCE flow. Tokens are cached in the SecureStore
so you don't need to re-authorize in a browser on every run. Access tokens
are refreshed silently when possible; a full browser flow only happens when
there's no usable cached/refreshable token.

Scope is a fixed broad default (DEFAULT_SCOPES below) rather than something
read from options.txt -- it intentionally requests more than a
presence-only build needs so the same grant covers future voice/video/
screenshare RPC features without re-prompting the user.
"""

import base64
import hashlib
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlencode, urlparse, parse_qs

import requests

from src.ropysence.core.logging_setup import get_logger
from src.ropysence.core.secure_store import SecureStore

log = get_logger("discord_oauth")

DEFAULT_SCOPES = (
    "rpc.activities.write "
    "rpc.screenshare.write rpc.screenshare.read "
    "rpc.video.write rpc.video.read "
    "rpc.voice.write rpc.voice.read "
    "rpc.notifications.read "
    "rpc "
    "sdk.social_layer_presence sdk.social_layer "
    "openid"
)

AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
TOKEN_URL = "https://discord.com/api/v10/oauth2/token"

STORE_KEY = "discord_tokens"  # {"access_token", "refresh_token", "expires_at", "scope"}


def _make_pkce_pair():
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _capture_redirect(expected_state: str, port: int) -> str:
    result = {}
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            qs = parse_qs(urlparse(self.path).query)
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            if "error" in qs:
                log.error("Discord returned OAuth error: %s", qs["error"][0])
                self.wfile.write(b"<h1>Authorization denied. You can close this tab.</h1>")
                result["error"] = qs["error"][0]
            else:
                log.info("redirect received with authorization code")
                self.wfile.write(b"<h1>Authorized! You can close this tab.</h1>")
                result["code"] = qs.get("code", [None])[0]
                result["state"] = qs.get("state", [None])[0]
            done.set()

        def log_message(self, *args):
            pass

    log.debug("starting local redirect-capture server on 127.0.0.1:%d", port)
    try:
        server = HTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        log.error("could not bind local server on port %d: %s", port, e)
        raise

    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    log.info("waiting for browser redirect (timeout=120s)...")

    if not done.wait(timeout=120):
        server.server_close()
        log.error("timed out waiting for OAuth redirect -- did the browser open?")
        raise RuntimeError("Timed out waiting for authorization redirect")
    server.server_close()

    if "error" in result:
        raise RuntimeError(f"Authorization failed: {result['error']}")
    if result.get("state") != expected_state:
        log.error("OAuth state mismatch -- possible interception, aborting")
        raise RuntimeError("OAuth state mismatch")
    if not result.get("code"):
        log.error("no authorization code present in redirect")
        raise RuntimeError("No authorization code received")

    return result["code"]


def _full_authorize(client_id: str, scopes: str, port: int) -> dict:
    log.info("starting full OAuth2 browser authorization flow (scopes='%s')", scopes)
    verifier, challenge = _make_pkce_pair()
    state = secrets.token_urlsafe(16)
    redirect_uri = f"http://127.0.0.1:{port}/callback"

    params = {
        "client_id": client_id, "response_type": "code", "redirect_uri": redirect_uri,
        "scope": scopes, "state": state, "code_challenge_method": "S256", "code_challenge": challenge,
    }
    url = f"{AUTHORIZE_URL}?{urlencode(params)}"
    log.info("opening browser for Discord authorization (client_id=%s...)", str(client_id)[:8])
    if not webbrowser.open(url):
        log.warning("webbrowser.open() reported failure -- open this URL manually:\n%s", url)

    code = _capture_redirect(state, port)
    log.debug("authorization code obtained (length=%d)", len(code))

    log.info("exchanging authorization code for tokens")
    try:
        resp = requests.post(TOKEN_URL, data={
            "client_id": client_id, "grant_type": "authorization_code", "code": code,
            "redirect_uri": redirect_uri, "code_verifier": verifier,
        }, timeout=15)
    except requests.RequestException as e:
        log.error("network failure during token exchange: %s", e)
        raise

    if resp.status_code != 200:
        log.error("token exchange failed (HTTP %d): %s", resp.status_code, resp.text[:300])
        resp.raise_for_status()

    tokens = resp.json()
    log.info("token exchange succeeded (scope=%s, expires_in=%ss)", tokens.get("scope"), tokens.get("expires_in"))
    return tokens


def _refresh(client_id: str, refresh_token: str) -> dict:
    log.info("attempting to refresh Discord access token")
    try:
        resp = requests.post(TOKEN_URL, data={
            "client_id": client_id, "grant_type": "refresh_token", "refresh_token": refresh_token,
        }, timeout=15)
    except requests.RequestException as e:
        log.warning("network failure during refresh (%s) -- falling back to full re-authorization", e)
        raise RuntimeError("refresh failed") from e

    if resp.status_code != 200:
        log.warning("token refresh failed (HTTP %d) -- falling back to full re-authorization", resp.status_code)
        raise RuntimeError("refresh failed")

    log.info("token refresh succeeded")
    return resp.json()


def get_access_token(client_id: str, scopes: str, port: int, store: SecureStore) -> str:
    """Returns a valid access token, preferring cached/refreshed tokens over
    a full browser authorization. Re-authorizes fresh if the cached grant's
    scope no longer matches the requested scopes (e.g. you changed
    discord_scopes in options.txt since the last run)."""
    cached = store.get(STORE_KEY)
    now = time.time()

    if cached and set(cached.get("scope", "").split()) != set(scopes.split()):
        log.warning("requested scopes changed since last run -- discarding cached tokens and re-authorizing")
        cached = None

    if cached and cached.get("expires_at", 0) > now + 60:
        log.info("using cached Discord access token (valid for %.0fs more)", cached["expires_at"] - now)
        return cached["access_token"]

    if cached and cached.get("refresh_token"):
        log.info("cached Discord access token expired or near-expiry, refreshing")
        try:
            tokens = _refresh(client_id, cached["refresh_token"])
        except RuntimeError:
            tokens = _full_authorize(client_id, scopes, port)
    else:
        log.info("no usable cached Discord tokens found, starting full authorization")
        tokens = _full_authorize(client_id, scopes, port)

    expires_at = now + tokens.get("expires_in", 0)
    refresh_token = tokens.get("refresh_token") or (cached.get("refresh_token") if cached else None)
    if not refresh_token:
        log.warning("no refresh_token available -- next run will require full re-authorization")

    store.set(STORE_KEY, {
        "access_token": tokens["access_token"],
        "refresh_token": refresh_token,
        "expires_at": expires_at,
        "scope": tokens.get("scope", scopes),
    })
    log.info("Discord tokens persisted to secure store (expires in %ss)", tokens.get("expires_in"))
    return tokens["access_token"]
