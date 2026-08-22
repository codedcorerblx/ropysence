"""
Turns a Roblox presence snapshot into a Discord Rich Presence activity dict,
fully driven by options.txt (see core/options.py for every key).

State handling:
  Offline  -> rpc.game.details.offline, default icon (large) + your Roblox
              profile picture (small, unless privacy.anonymous), profile
              button only
  Online   -> same treatment as Offline
  Studio   -> same treatment as Offline
  In game  -> real game icon (large, proxied) + your avatar (small) + up to
              2 buttons (join, then profile/gamepage), unless
              privacy.anonymous, in which case it behaves like a generic
              "in game" state with no game name, no counts, no buttons, no
              avatar, default icon only.

Button slot logic (Discord allows max 2 buttons):
  slot 1: Join (only when actually in a specific joinable game)
  slot 2: Profile if rpc.button.profile is on, else Gamepage if
          rpc.button.gamepage is on (gamepage is a fallback for when
          profile is deliberately turned off, not an equal alternative)
Outside of "in game", join/gamepage have no target to link to, so only
profile is ever offered there.
"""

import json
import time

from src.core.logging_setup import get_logger
from src.core.templating import render
from src.discord.assets import proxy_image_urls
from src.roblox.client import (
    RobloxClient,
    PRESENCE_OFFLINE, PRESENCE_ONLINE, PRESENCE_INGAME,
    PRESENCE_INSTUDIO, PRESENCE_INVISIBLE,
)

log = get_logger("presence_builder")


def _fetch_game_chain(roblox: RobloxClient, place_id):
    """universeId -> game details + icon URL. Sequential dependency inside
    this one worker task; runs concurrently with everything else via the
    caller's WorkerPool."""
    universe_id = roblox.get_universe_id(place_id) if place_id else None
    if not universe_id:
        return None, {}, None
    game_details = roblox.get_game_details(universe_id)
    icon_url = roblox.get_game_icon_url(universe_id)
    return universe_id, game_details, icon_url


class PresenceBuilder:
    def __init__(self, roblox: RobloxClient, user: dict, options: dict, access_token: str, client_id: str):
        self.roblox = roblox
        self.user = user  # {"id": ..., "name": ..., "displayName": ...}
        self.opt = options
        self.access_token = access_token
        self.client_id = client_id
        self._state_tracker = {}  # persists across polls -- powers the timestamp-continuity fix

    def _base_context(self) -> dict:
        return {
            "user.name": self.user.get("name"),
            "user.display": self.user.get("displayName") or self.user.get("name"),
        }

    def _profile_url(self) -> str:
        return f"https://www.roblox.com/users/{self.user['id']}/profile"

    def _select_buttons(self, join_url: str | None, gamepage_url: str | None, context: dict) -> list:
        """Returns a list of {"label","url","is_profile"} dicts, at most 2,
        per the slot-priority rules documented at module level."""
        buttons = []
        if join_url and self.opt["rpc.button.join"]:
            buttons.append({
                "label": render(self.opt["rpc.button.join.content"], context),
                "url": join_url, "is_profile": False,
            })

        if self.opt["rpc.button.profile"]:
            buttons.append({
                "label": render(self.opt["rpc.button.profile.content"], context),
                "url": self._profile_url(), "is_profile": True,
            })
        elif gamepage_url and self.opt["rpc.button.gamepage"]:
            buttons.append({
                "label": render(self.opt["rpc.button.gamepage.content"], context),
                "url": gamepage_url, "is_profile": False,
            })

        return buttons[:2]

    def build(self):
        """Returns a Discord activity dict, or None if this cycle should be
        skipped entirely (rate limited / fetch failed)."""
        uid = self.user["id"]
        anonymous = self.opt["privacy.anonymous"]

        default_icon_value = self.opt["script.dev.img.default"]
        default_is_url = bool(default_icon_value) and default_icon_value.startswith(("http://", "https://"))
        # If it's a URL (e.g. the hosted icon.png), it goes through the same
        # proxy mechanism as every other image -- confirmed working. If it's
        # a bare string, treat it as a literal Rich Presence Art Asset key
        # (requires manual upload in the dev portal, no proxying needed).

        try:
            presence = self.roblox.get_presence(uid)
        except TimeoutError:
            log.warning("skipping this poll cycle due to rate limiting")
            return None
        except Exception as e:
            log.error("failed to fetch presence, skipping this cycle: %s", e)
            return None

        ptype = presence.get("userPresenceType", PRESENCE_OFFLINE)
        context = self._base_context()

        # Large image: the game/app icon (default icon.png for non-game
        # states, the real Roblox game thumbnail while in-game).
        # Small image: your Roblox profile picture, shown everywhere --
        # same avatar "in game" already showed -- unless anonymous mode
        # is on, in which case no avatar is ever fetched or shown.
        large_image = None if default_is_url else (default_icon_value or None)
        large_image_url_to_proxy = default_icon_value if default_is_url else None
        small_image_url_to_proxy = None if anonymous else self.roblox.get_user_headshot_url(uid)
        buttons = []
        state = ""

        if ptype in (PRESENCE_OFFLINE, PRESENCE_ONLINE, PRESENCE_INVISIBLE, PRESENCE_INSTUDIO):
            if ptype == PRESENCE_OFFLINE:
                log.debug("presence: offline")
                details = render(self.opt["rpc.game.details.offline"], context)
            elif ptype == PRESENCE_INSTUDIO:
                log.info("presence: in Roblox Studio")
                details = render(self.opt["rpc.game.details.studio"], context)
            else:
                log.debug("presence: online (website), type=%s", ptype)
                details = render(self.opt["rpc.game.details.online"], context)

            buttons = self._select_buttons(None, None, context)

        elif ptype == PRESENCE_INGAME:
            place_id = presence.get("placeId")
            game_id = presence.get("gameId")
            log.info("presence: in game, placeId=%s gameId=%s anonymous=%s", place_id, game_id, anonymous)

            join_url = f"roblox://placeId={place_id}&gameInstanceId={game_id}" if (place_id and game_id) else None
            gamepage_url = f"https://www.roblox.com/games/{place_id}" if place_id else None

            if anonymous:
                details = render(self.opt["rpc.game.details.anonymous"], context)
                # state stays "", no buttons, no avatar (already excluded
                # above), large image stays the configured default -- the
                # whole point of anonymous mode.
            else:
                _, game_details, icon_url = self._fetch_game_data(place_id)
                game_name = game_details.get("name", "a game")
                context["game.name"] = game_name
                state = render(self.opt["rpc.game.state"], context)

                if icon_url:
                    # a real per-game icon takes priority over the default
                    large_image_url_to_proxy = icon_url
                    large_image = None

                match = None
                if self.opt["privacy.player.count"] and place_id and game_id:
                    match = self.roblox.find_matching_server(place_id, game_id, 5)

                if match:
                    current, mx = match
                    match_context = dict(context)
                    match_context["game.server.current"] = current
                    match_context["game.server.max"] = mx
                    match_context["game.server.min"] = game_details.get("minPlayers")  # Roblox rarely exposes this
                    details = render(self.opt["rpc.game.details"], match_context)
                else:
                    unmatched_context = dict(context)
                    if not self.opt["privacy.player.name.placeholder"]:
                        unmatched_context["user.name"] = None  # blank it out, privacy default
                    details = render(self.opt["rpc.game.details.unmatched"], unmatched_context)

                buttons = self._select_buttons(join_url, gamepage_url, context)

        else:
            log.warning("unrecognized presence type=%s, treating as offline", ptype)
            details = render(self.opt["rpc.game.details.offline"], context)
            buttons = self._select_buttons(None, None, context)

        # Resolve any raw URLs (avatar, Roblox game icon, or the default
        # icon) into Discord-usable "mp:..." refs, one batched call.
        proxied = {}
        urls_needing_proxy = [u for u in (large_image_url_to_proxy, small_image_url_to_proxy) if u]
        if urls_needing_proxy:
            proxied = proxy_image_urls(self.access_token, self.client_id, urls_needing_proxy)

        if large_image_url_to_proxy:
            large_image = proxied.get(large_image_url_to_proxy)
            if not large_image:
                log.warning(
                    "image proxy failed for %s -- trying configured default icon as a second fallback",
                    large_image_url_to_proxy,
                )
                if default_is_url and default_icon_value and default_icon_value != large_image_url_to_proxy:
                    fallback = proxy_image_urls(self.access_token, self.client_id, [default_icon_value])
                    large_image = fallback.get(default_icon_value)
                elif not default_is_url and default_icon_value:
                    large_image = default_icon_value
                if not large_image:
                    log.warning("default icon fallback also unavailable -- omitting large image this cycle")

        small_image = proxied.get(small_image_url_to_proxy) if small_image_url_to_proxy else None
        if small_image_url_to_proxy and not small_image:
            log.warning("avatar proxy failed for %s -- omitting", small_image_url_to_proxy)

        # Only reset the elapsed-time counter when the state actually
        # changes, not on every poll cycle.
        signature = (ptype, presence.get("placeId"), presence.get("gameId"))
        if self._state_tracker.get("signature") != signature:
            self._state_tracker["signature"] = signature
            self._state_tracker["start_ms"] = int(time.time() * 1000)
            log.info("presence state changed (%s) -- resetting elapsed-time counter", signature)
        else:
            log.debug("presence state unchanged -- keeping existing start timestamp")

        activity = {
            "name": self.opt["rpc.game.name"],
            "type": self.opt["rpc.type"],
            "application_id": self.client_id,
            "details": details,
            "timestamps": {"start": self._state_tracker["start_ms"]},
            "assets": {},
        }
        if state:
            # Omit rather than send an empty string -- Online/Studio/Offline
            # have no game-name context, and an absent key is a smaller
            # surface for something to be interpreted as "invalid" than a
            # present-but-empty one.
            activity["state"] = state
        if large_image:
            activity["assets"]["large_image"] = large_image
            activity["assets"]["large_text"] = state or details
        if small_image:
            activity["assets"]["small_image"] = small_image
            activity["assets"]["small_text"] = self.user.get("name")
        if buttons:
            activity["buttons"] = [b["label"] for b in buttons]
            activity["metadata"] = {"button_urls": [b["url"] for b in buttons]}
            if state:
                for b in buttons:
                    if b.get("is_profile"):
                        activity["state_url"] = b["url"]
                        break

        if not activity["assets"]:
            del activity["assets"]

        log.info("build_activity: result details='%s' state='%s'", details, state)
        log.debug("full activity payload: %s", json.dumps(activity))
        return activity

    def _fetch_game_data(self, place_id):
        return _fetch_game_chain(self.roblox, place_id)
