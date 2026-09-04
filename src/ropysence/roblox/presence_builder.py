"""
Turns a Roblox presence snapshot into a Discord Rich Presence activity dict,
fully driven by options.txt (see core/options.py for every key).

Buttons: exactly two generic slots (Discord's own limit), each just a
text+url template pair -- rpc.button.one.{text,url} and
rpc.button.two.{text,url}. A button is included only if BOTH its text and
url fully resolve (no missing placeholder); this is what makes the default
Join button ({game.id}/{game.instance} in its URL) only appear while
actually in a game, without any special-cased "is this a join button" logic
anywhere -- it's an emergent property of the template, so it applies
equally to whatever the user reconfigures either slot to.

Custom placeholders (placeholder.<name>="..." in options.txt, referenced as
{custom.<name>}) are resolved once per build() call against your Roblox
user info (name/display/id) -- NOT per-game dynamic tokens like
{game.name}, since those aren't known yet at that point in the flow. A
button template can still reference {game.id} etc. directly just fine;
only a *custom placeholder's own* definition is limited to user-level info.

State handling:
  Offline  -> rpc.game.details.offline, default icon (large) + your Roblox
              profile picture (small, unless privacy.anonymous)
  Online   -> same treatment as Offline
  Studio   -> same treatment as Offline
  In game  -> real game icon (large, proxied) + your avatar (small), unless
              privacy.anonymous, in which case it behaves like a generic
              "in game" state with no game name, no counts, no buttons, no
              avatar, default icon only.
"""

import json
import time

from src.ropysence.core.logging_setup import get_logger
from src.ropysence.core.templating import render, render_track_missing, resolve_custom_placeholders
from src.ropysence.discord.assets import proxy_image_urls
from src.ropysence.roblox.client import (
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
    def __init__(self, roblox: RobloxClient, user: dict, options: dict, get_access_token_fn, client_id: str, human_notifier=None):
        self.roblox = roblox
        self.user = user  # {"id": ..., "name": ..., "displayName": ...}
        self.opt = options
        self.get_access_token_fn = get_access_token_fn  # zero-arg callable -- always fetches the CURRENT token,
        # never a frozen one from construction time. Matters once the Gateway
        # can run for days across reconnects: image-proxy calls need a token
        # that's still valid even long after the process started.
        self.client_id = client_id
        self.human_notifier = human_notifier  # HumanWebhookNotifier or None
        self._state_tracker = {}  # persists across polls -- powers the timestamp-continuity fix

    def _base_context(self) -> dict:
        return {
            "user.name": self.user.get("name"),
            "user.display": self.user.get("displayName") or self.user.get("name"),
            "user.id": self.user.get("id"),
        }

    def _build_button(self, number: str, context: dict):
        """number is 'one' or 'two'. Returns {"label", "url"} or None if
        either the text or URL references a placeholder that isn't
        available right now -- e.g. the default Join button needs
        {game.id}/{game.instance}, which only exist while actually in a
        game. This is a generic rule, not special-cased per button, so it
        applies the same way to whatever the user reconfigures either slot to."""
        text_template = self.opt[f"rpc.button.{number}.text"]
        url_template = self.opt[f"rpc.button.{number}.url"]
        label, label_missing = render_track_missing(text_template, context)
        url, url_missing = render_track_missing(url_template, context)
        if not label or not url or label_missing or url_missing:
            log.debug("button '%s' skipped this cycle (missing placeholder or empty result)", number)
            return None
        return {"label": label, "url": url}

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
        context.update(resolve_custom_placeholders(self.opt.get("_custom_placeholders", {}), context))

        # Large image: the game/app icon (default icon.png for non-game
        # states, the real Roblox game thumbnail while in-game).
        # Small image: your Roblox profile picture, shown everywhere --
        # same avatar "in game" already showed -- unless anonymous mode
        # is on, in which case no avatar is ever fetched or shown.
        large_image = None if default_is_url else (default_icon_value or None)
        large_image_url_to_proxy = default_icon_value if default_is_url else None
        small_image_url_to_proxy = None if anonymous else self.roblox.get_user_headshot_url(uid)
        state = ""
        human_message = None  # set per-branch below, only ever sent on a genuine state change

        if ptype in (PRESENCE_OFFLINE, PRESENCE_ONLINE, PRESENCE_INVISIBLE, PRESENCE_INSTUDIO):
            if ptype == PRESENCE_OFFLINE:
                log.debug("presence: offline")
                details = render(self.opt["rpc.game.details.offline"], context)
                human_message = render(self.opt["human.message.offline"], context)
            elif ptype == PRESENCE_INSTUDIO:
                log.info("presence: in Roblox Studio")
                details = render(self.opt["rpc.game.details.studio"], context)
                human_message = render(self.opt["human.message.studio"], context)
            else:
                log.debug("presence: online (website), type=%s", ptype)
                details = render(self.opt["rpc.game.details.online"], context)
                human_message = render(self.opt["human.message.online"], context)

        elif ptype == PRESENCE_INGAME:
            place_id = presence.get("placeId")
            game_id = presence.get("gameId")
            log.info("presence: in game, placeId=%s gameId=%s anonymous=%s", place_id, game_id, anonymous)

            if anonymous:
                details = render(self.opt["rpc.game.details.anonymous"], context)
                human_message = render(self.opt["human.message.ingame.anonymous"], context)
                # state stays "", no buttons, no avatar (already excluded
                # above), large image stays the configured default -- the
                # whole point of anonymous mode.
            else:
                universe_id, game_details, icon_url = self._fetch_game_data(place_id)
                game_name = game_details.get("name", "a game")
                context["game.name"] = game_name
                # {game.id} = Roblox placeId (the game itself); {game.instance}
                # = Roblox gameId (the specific server/job the player is on --
                # Roblox's own field naming is a little confusing here, since
                # "gameId" is actually the server instance, not the game).
                context["game.id"] = place_id
                context["game.instance"] = game_id

                # Subplace: only set when the current place differs from
                # the universe's main/root place. Blank otherwise -- this
                # covers both "not a subplace" and "we couldn't check"
                # (develop.roblox.com's places endpoint requires edit
                # access to the game, so it 403s for most games you don't
                # own; get_universe_places() handles that gracefully).
                root_place_id = game_details.get("rootPlaceId")
                if universe_id and place_id and root_place_id and place_id != root_place_id:
                    places = self.roblox.get_universe_places(universe_id)
                    subplace_name = places.get(place_id) if places else None
                    if subplace_name:
                        context["game.subplace.id"] = place_id
                        context["game.subplace.name"] = subplace_name
                        log.info("on a subplace: '%s' (placeId=%s, root=%s)", subplace_name, place_id, root_place_id)
                    else:
                        log.debug(
                            "on a non-root place (placeId=%s != rootPlaceId=%s) but its name wasn't "
                            "available -- {game.subplace.*} left blank", place_id, root_place_id,
                        )

                state = render(self.opt["rpc.game.state"], context)

                if icon_url:
                    # a real per-game icon takes priority over the default
                    large_image_url_to_proxy = icon_url
                    large_image = None

                match = None
                if self.opt["privacy.player.count"] and place_id and game_id:
                    match = self.roblox.find_matching_server(place_id, game_id, 5)

                human_context = dict(context)  # picks up game.subplace.* too, if set above
                if match:
                    current, mx = match
                    match_context = dict(context)
                    match_context["game.server.current"] = current
                    match_context["game.server.max"] = mx
                    match_context["game.server.min"] = game_details.get("minPlayers")  # Roblox rarely exposes this
                    details = render(self.opt["rpc.game.details"], match_context)
                    human_context.update(match_context)
                else:
                    unmatched_context = dict(context)
                    if not self.opt["privacy.player.name.placeholder"]:
                        unmatched_context["user.name"] = None  # blank it out, privacy default
                    details = render(self.opt["rpc.game.details.unmatched"], unmatched_context)

                human_message = render(self.opt["human.message.ingame"], human_context)

        else:
            log.warning("unrecognized presence type=%s, treating as offline", ptype)
            details = render(self.opt["rpc.game.details.offline"], context)
            human_message = render(self.opt["human.message.offline"], context)

        buttons = []
        if not anonymous:
            for number in ("one", "two"):
                b = self._build_button(number, context)
                if b:
                    buttons.append(b)

        # Resolve any raw URLs (avatar, Roblox game icon, or the default
        # icon) into Discord-usable "mp:..." refs, one batched call.
        proxied = {}
        urls_needing_proxy = [u for u in (large_image_url_to_proxy, small_image_url_to_proxy) if u]
        if urls_needing_proxy:
            access_token = self.get_access_token_fn()
            proxied = proxy_image_urls(access_token, self.client_id, urls_needing_proxy)

        if large_image_url_to_proxy:
            large_image = proxied.get(large_image_url_to_proxy)
            if not large_image:
                log.warning(
                    "image proxy failed for %s -- trying configured default icon as a second fallback",
                    large_image_url_to_proxy,
                )
                if default_is_url and default_icon_value and default_icon_value != large_image_url_to_proxy:
                    fallback = proxy_image_urls(self.get_access_token_fn(), self.client_id, [default_icon_value])
                    large_image = fallback.get(default_icon_value)
                elif not default_is_url and default_icon_value:
                    large_image = default_icon_value
                if not large_image:
                    log.warning("default icon fallback also unavailable -- omitting large image this cycle")

        small_image = proxied.get(small_image_url_to_proxy) if small_image_url_to_proxy else None
        if small_image_url_to_proxy and not small_image:
            log.warning("avatar proxy failed for %s -- omitting", small_image_url_to_proxy)

        # Only reset the elapsed-time counter when the state actually
        # changes, not on every poll cycle. The human-readable webhook
        # notification (if configured) fires here too, for the same
        # reason: once per real transition, never every poll.
        signature = (ptype, presence.get("placeId"), presence.get("gameId"))
        state_changed = self._state_tracker.get("signature") != signature
        if state_changed:
            self._state_tracker["signature"] = signature
            self._state_tracker["start_ms"] = int(time.time() * 1000)
            log.info("presence state changed (%s) -- resetting elapsed-time counter", signature)
            if self.human_notifier and self.human_notifier.enabled and human_message:
                self.human_notifier.notify(human_message)
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
            if state and len(buttons) > 1:
                activity["state_url"] = buttons[1]["url"]

        if not activity["assets"]:
            del activity["assets"]

        log.info("build_activity: result details='%s' state='%s' buttons=%s", details, state, [b["label"] for b in buttons])
        log.debug("full activity payload: %s", json.dumps(activity))
        return activity

    def _fetch_game_data(self, place_id):
        return _fetch_game_chain(self.roblox, place_id)
