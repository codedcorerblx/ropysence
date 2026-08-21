"""
Thin wrapper around the (undocumented but widely used) Roblox web APIs
needed to build a "now playing" status:

  users.roblox.com       -> verify cookie, get user id/name
  presence.roblox.com    -> current presence (offline/online/in-game/in-studio)
  apis.roblox.com        -> placeId -> universeId
  games.roblox.com       -> game name/details, public server listing
  thumbnails.roblox.com  -> game icon, user avatar headshot

The cookie is the only credential involved. It is held in memory on the
requests.Session and is NEVER written to a log line -- only its presence/
absence, or the resulting username, is logged. Response BODIES are logged
in full at DBG (they're not sensitive -- just game/presence data), which is
your fastest way to see exactly what Roblox is telling us; see
tools/diagnose_presence.py for an even faster standalone check.
"""

import threading

import requests

from src.core.logging_setup import get_logger
from src.workers.pool import WorkerPool

log = get_logger("roblox_client")

AUTH_URL = "https://users.roblox.com/v1/users/authenticated"
PRESENCE_URL = "https://presence.roblox.com/v1/presence/users"
UNIVERSE_URL = "https://apis.roblox.com/universes/v1/places/{place_id}/universe"
GAME_DETAILS_URL = "https://games.roblox.com/v1/games"
GAME_ICON_URL = "https://thumbnails.roblox.com/v1/games/icons"
USER_HEADSHOT_URL = "https://thumbnails.roblox.com/v1/users/avatar-headshot"
SERVERS_URL = "https://games.roblox.com/v1/games/{place_id}/servers/Public"

PRESENCE_OFFLINE = 0
PRESENCE_ONLINE = 1
PRESENCE_INGAME = 2
PRESENCE_INSTUDIO = 3
PRESENCE_INVISIBLE = 4


class RobloxAuthError(Exception):
    """Cookie missing, expired, or rejected by Roblox."""


class RobloxClient:
    def __init__(self, cookie: str, user_agent: str = "Mozilla/5.0", max_workers: int = 4):
        if not cookie:
            raise ValueError("Roblox cookie is required")
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": user_agent})
        self._session.cookies.set(".ROBLOSECURITY", cookie, domain=".roblox.com")

        # CSRF token is the only piece of session state mutated after init,
        # and it can be written from multiple worker threads at once (two
        # concurrent presence calls both getting a 403 on a stale token) --
        # guard read-modify-write with a lock. requests.Session's underlying
        # cookiejar is internally lock-protected by http.cookiejar, so plain
        # concurrent GETs/POSTs that don't touch _csrf_token are fine as-is.
        self._csrf_token = None
        self._csrf_lock = threading.Lock()

        self.pool = WorkerPool(max_workers=max_workers, thread_name_prefix="roblox-worker")

        self._universe_cache = {}
        self._game_details_cache = {}
        self._icon_cache = {}
        self._headshot_cache = {}

        log.info("RobloxClient initialized (cookie loaded into session, value never logged, worker pool size=%d)", max_workers)

    def close(self):
        self.pool.shutdown()

    # -- internal request helpers --------------------------------------------

    def _get(self, url, **kwargs):
        log.debug("GET %s params=%s", url, kwargs.get("params"))
        try:
            resp = self._session.get(url, timeout=10, **kwargs)
        except requests.RequestException as e:
            log.error("network failure on GET %s: %s", url, e)
            raise
        log.debug("GET %s -> HTTP %d, body=%s", url, resp.status_code, resp.text[:500])
        return resp

    def _post_with_csrf(self, url, json_body):
        with self._csrf_lock:
            token = self._csrf_token
        headers = {"X-CSRF-TOKEN": token} if token else {}
        log.debug("POST %s body=%s (csrf=%s)", url, json_body, "set" if token else "none yet")

        try:
            resp = self._session.post(url, json=json_body, headers=headers, timeout=10)
        except requests.RequestException as e:
            log.error("network failure on POST %s: %s", url, e)
            raise

        if resp.status_code == 403 and "x-csrf-token" in resp.headers:
            new_token = resp.headers["x-csrf-token"]
            with self._csrf_lock:
                changed = new_token != self._csrf_token
                if changed:
                    self._csrf_token = new_token
            if changed:
                log.info("received a fresh CSRF token from Roblox, retrying request")
                headers["X-CSRF-TOKEN"] = new_token
                try:
                    resp = self._session.post(url, json=json_body, headers=headers, timeout=10)
                except requests.RequestException as e:
                    log.error("network failure retrying POST %s after CSRF refresh: %s", url, e)
                    raise

        log.debug("POST %s -> HTTP %d, body=%s", url, resp.status_code, resp.text[:500])
        return resp

    # -- public API -----------------------------------------------------------

    def get_authenticated_user(self) -> dict:
        log.info("verifying Roblox cookie against authenticated-user endpoint")
        resp = self._get(AUTH_URL)

        if resp.status_code == 401:
            log.error("Roblox cookie rejected (HTTP 401) -- expired, invalid, or session ended elsewhere")
            raise RobloxAuthError("Roblox cookie is invalid or expired")
        if resp.status_code != 200:
            log.error("unexpected response verifying cookie: HTTP %d: %s", resp.status_code, resp.text[:200])
            raise RobloxAuthError(f"Unexpected status {resp.status_code} verifying cookie")

        data = resp.json()
        log.info("cookie verified -- authenticated as %s (uid=%s)", data.get("name"), data.get("id"))
        return data

    def get_presence(self, user_id: int) -> dict:
        log.debug("fetching presence for uid=%s (requested exactly this id, double-check it matches your account)", user_id)
        resp = self._post_with_csrf(PRESENCE_URL, {"userIds": [user_id]})

        if resp.status_code == 401:
            log.error("presence fetch rejected -- cookie no longer valid")
            raise RobloxAuthError("Roblox cookie is invalid or expired")
        if resp.status_code == 429:
            log.warning("hit Roblox rate limit (HTTP 429) on presence endpoint -- backing off this cycle")
            raise TimeoutError("rate limited")
        if resp.status_code != 200:
            log.error("presence fetch failed: HTTP %d: %s", resp.status_code, resp.text[:300])
            raise RuntimeError(f"presence fetch failed with HTTP {resp.status_code}")

        body = resp.json()
        log.debug("full presence response for uid=%s: %s", user_id, body)
        presences = body.get("userPresences", [])
        if not presences:
            log.warning("presence response had NO entries at all for uid=%s (full body above) -- treating as offline", user_id)
            return {"userPresenceType": PRESENCE_OFFLINE}

        p = presences[0]
        ptype = p.get("userPresenceType")
        log.debug(
            "presence type=%s placeId=%s gameId=%s lastLocation=%r lastOnline=%s",
            ptype, p.get("placeId"), p.get("gameId"), p.get("lastLocation"), p.get("lastOnline"),
        )

        if ptype == PRESENCE_OFFLINE:
            log.warning(
                "Roblox reports this account as OFFLINE (userPresenceType=0). If you expect "
                "otherwise, the full response body was logged above at DBG -- check it first "
                "(an empty/odd body means Roblox itself thinks you're offline, not a bug here). "
                "Things worth checking if the body genuinely says offline while you're playing: "
                "(1) Settings > Privacy > 'Who can see that you're online' -- some privacy levels "
                "affect presence reads even for your own cookie; (2) the cookie belongs to the "
                "session that's actually in-game (e.g. a cookie copied from a browser tab, while "
                "you're playing via the desktop app under a *different* logged-in session); "
                "(3) presence.roblox.com has a known short caching lag after you join a game, "
                "usually well under a minute, not persistent."
            )

        return p

    def get_universe_id(self, place_id: int):
        if place_id in self._universe_cache:
            return self._universe_cache[place_id]
        resp = self._get(UNIVERSE_URL.format(place_id=place_id))
        if resp.status_code != 200:
            log.warning("could not resolve universeId for placeId=%s (HTTP %d)", place_id, resp.status_code)
            return None
        universe_id = resp.json().get("universeId")
        self._universe_cache[place_id] = universe_id
        log.info("resolved placeId=%s -> universeId=%s", place_id, universe_id)
        return universe_id

    def get_game_details(self, universe_id: int) -> dict:
        if universe_id in self._game_details_cache:
            return self._game_details_cache[universe_id]
        resp = self._get(GAME_DETAILS_URL, params={"universeIds": universe_id})
        if resp.status_code != 200:
            log.warning("could not fetch game details for universeId=%s (HTTP %d)", universe_id, resp.status_code)
            return {}
        data = resp.json().get("data", [])
        details = data[0] if data else {}
        self._game_details_cache[universe_id] = details
        log.info("game details cached: '%s' (universeId=%s)", details.get("name"), universe_id)
        return details

    def get_game_icon_url(self, universe_id: int):
        if universe_id in self._icon_cache:
            return self._icon_cache[universe_id]
        resp = self._get(GAME_ICON_URL, params={
            "universeIds": universe_id, "size": "512x512", "format": "Png", "isCircular": "false",
        })
        if resp.status_code != 200:
            log.warning(
                "could not fetch game icon for universeId=%s (HTTP %d) -- large image will be omitted",
                universe_id, resp.status_code,
            )
            return None
        data = resp.json().get("data", [])
        url = data[0]["imageUrl"] if data else None
        self._icon_cache[universe_id] = url
        log.debug("game icon cached for universeId=%s", universe_id)
        return url

    def get_user_headshot_url(self, user_id: int):
        if user_id in self._headshot_cache:
            return self._headshot_cache[user_id]
        resp = self._get(USER_HEADSHOT_URL, params={
            "userIds": user_id, "size": "150x150", "format": "Png", "isCircular": "false",
        })
        if resp.status_code != 200:
            log.warning(
                "could not fetch avatar headshot for uid=%s (HTTP %d) -- small image will be omitted",
                user_id, resp.status_code,
            )
            return None
        data = resp.json().get("data", [])
        url = data[0]["imageUrl"] if data else None
        self._headshot_cache[user_id] = url
        log.debug("avatar headshot cached for uid=%s", user_id)
        return url

    def find_matching_server(self, place_id: int, game_id: str, max_pages: int = 5):
        """Paginate the public server list for `place_id` looking for `game_id`.
        Returns (current_players, max_players) or None if not found within
        max_pages -- which is expected/normal for private or friends-only
        servers, not an error."""
        cursor = ""
        for page in range(1, max_pages + 1):
            params = {"sortOrder": "Asc", "limit": 100}
            if cursor:
                params["cursor"] = cursor

            resp = self._get(SERVERS_URL.format(place_id=place_id), params=params)
            if resp.status_code != 200:
                log.warning("server list page %d failed (HTTP %d), aborting search", page, resp.status_code)
                return None

            body = resp.json()
            servers = body.get("data", [])
            log.debug("server list page %d: %d server(s)", page, len(servers))

            for s in servers:
                if s.get("id") == game_id:
                    log.info(
                        "matched server %s... on page %d -- %s/%s players",
                        str(game_id)[:8], page, s.get("playing"), s.get("maxPlayers"),
                    )
                    return s.get("playing"), s.get("maxPlayers")

            cursor = body.get("nextPageCursor")
            if not cursor:
                break

        log.warning(
            "no matching public server found for gameId=%s... within %d page(s) -- "
            "likely private/friends-only, falling back to username placeholder",
            str(game_id)[:8] if game_id else game_id, max_pages,
        )
        return None
