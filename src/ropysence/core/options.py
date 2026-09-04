"""
Loads (or creates) options.txt -- a flat `dotted.key=value` config format,
deliberately simpler than JSON for hand-editing. See OPTION_SCHEMA below for
every supported key, its type, default, and whether it's required.

Value syntax:
  bool     true / false               (case-insensitive)
  int      5, 8969, 0
  str      "quoted, may contain {placeholders}"   or bare word (quotes optional
           for str values, but recommended so leading/trailing spaces and
           punctuation are unambiguous)
  list     ["url1","url2"]            a JSON array -- used for webhook URLs
           so multiple can be configured (spreads load / avoids rate limits).
           A single bare/quoted URL is also accepted and wrapped into a
           one-item list, for convenience and backward compatibility.

Custom placeholders: any line of the form `placeholder.<anything>="value"`
defines a user placeholder, referenced elsewhere as {custom.<anything>} --
unlimited, name them anything. These are NOT part of OPTION_SCHEMA (the
whole point is the name is arbitrary), so they're parsed out separately in
load_options() and returned under the reserved "_custom_placeholders" key
as {name: raw_template} for src.core.templating.resolve_custom_placeholders()
to resolve fresh each poll cycle (they may reference dynamic built-in
tokens like {game.name}, so they can't be resolved once at load time).

Lines starting with # (after stripping leading whitespace) and blank lines
are ignored. Unknown keys (that aren't `placeholder.*`) are warned about,
not fatal, so a stray typo doesn't crash the whole config.
"""

import json
from pathlib import Path

from src.ropysence.core.logging_setup import get_logger
from src.ropysence.core.secure_store import APP_DIR

log = get_logger("options")

OPTIONS_FILE = APP_DIR / "options.txt"
CUSTOM_PLACEHOLDER_PREFIX = "placeholder."

PLACEHOLDER_HELP = (
    "Supported placeholders (usable inside any \"quoted\" content field):\n"
    "  {user.name}            Roblox username\n"
    "  {user.display}         Roblox display name\n"
    "  {user.id}               Roblox user id\n"
    "  {game.name}            Current game name\n"
    "  {game.id}               Roblox placeId of the current game\n"
    "  {game.instance}         Roblox server/instance id (job id) of the current game\n"
    "  {game.server.current}  Players currently on the matched server\n"
    "  {game.server.min}      Server's minimum player count (Roblox rarely exposes this -- often blank)\n"
    "  {game.server.max}      Server's maximum player count\n"
    "  {game.subplace.name}   Name of the specific subplace/area you're on, if the game has\n"
    "                          multiple places and you're not on the main one -- blank if you're\n"
    "                          on the main place, OR if this data isn't accessible (this endpoint\n"
    "                          only works for games you personally own/co-own, so it's blank for\n"
    "                          most third-party games -- that's expected, not a bug)\n"
    "  {game.subplace.id}     placeId of that subplace (same value as {game.id} when set -- its\n"
    "                          main use is as a blank/non-blank signal for whether you're on a\n"
    "                          subplace at all, e.g. \"{game.name}\" vs \"{game.name} | {game.subplace.name}\")\n"
    "Missing placeholders render as empty text, never an error. A button whose\n"
    "text or url ends up referencing a missing placeholder is hidden entirely\n"
    "rather than shown broken -- this is why the default Join button only\n"
    "appears while actually in a game (it needs {game.id}/{game.instance}).\n"
    "\n"
    "You can also define your own placeholders, unlimited, named anything:\n"
    "  placeholder.<name>=\"value\"   -- reference it as {custom.<name>}\n"
    "Custom placeholder values may themselves reference other placeholders,\n"
    "including other custom ones, in any order. Example:\n"
    "  placeholder.my.website=\"example.com\"\n"
    "  rpc.button.two.url=\"https://{custom.my.website}\""
)

# key -> (type, default, required, comment)
# type is one of "bool", "int", "str", "list"
OPTION_SCHEMA = {
    # --- Required ---
    "script.user.id": ("str", "", True, "Discord Application (Client) ID -- required"),
    "script.bot.id": ("str", "", False, "Reserved for future use -- not consumed by the current build, safe to leave blank"),

    # --- Privacy ---
    "privacy.player.count": ("bool", True, False, "Show current/max player counts for the matched server"),
    "privacy.player.name.placeholder": ("bool", False, False, "When counts can't be fetched, show your username in the fallback text instead of a blank"),
    "privacy.anonymous": ("bool", False, False, "Hide game name/counts/buttons entirely; large image becomes script.dev.img.default"),

    # --- Buttons (Discord allows max 2 -- exactly these two slots, fully customizable) ---
    "rpc.button.one.text": ("str", "Join Game", False, "Label for the first button"),
    "rpc.button.one.url": ("str", "roblox://placeId={game.id}&gameInstanceId={game.instance}", False, "URL for the first button. Default only resolves while actually in a game, so it's hidden the rest of the time -- see the placeholder notes above."),
    "rpc.button.two.text": ("str", "{user.name}'s Profile", False, "Label for the second button"),
    "rpc.button.two.url": ("str", "https://www.roblox.com/users/{user.id}/profile", False, "URL for the second button"),

    # --- Activity text ---
    "rpc.game.name": ("str", "Roblox", False, "Activity name shown at the top"),
    "rpc.game.details": ("str", "In Game ({game.server.current}/{game.server.max})", False, "Used when in a game AND a public server match with counts was found"),
    "rpc.game.details.unmatched": ("str", "In Game ({user.name})", False, "Used when in a game but no count is available (respects privacy.player.name.placeholder)"),
    "rpc.game.details.anonymous": ("str", "In Game", False, "Used when in a game AND privacy.anonymous is on"),
    "rpc.game.details.online": ("str", "Online", False, ""),
    "rpc.game.details.studio": ("str", "In Studio", False, ""),
    "rpc.game.details.offline": ("str", "Offline", False, ""),
    "rpc.game.state": ("str", "{game.name}", False, "Only used while in a game. If the game has subplaces you own/co-own, try e.g. \"{game.name} | {game.subplace.name}\" -- see {game.subplace.*} above"),
    "rpc.type": ("int", 0, False, "Discord activity type: 0 Playing, 1 Streaming, 2 Listening, 3 Watching, 5 Competing"),
    "rpc.state": ("str", "online", False, "Discord status shown alongside the activity: online / idle / dnd (invisible is not supported)"),

    # --- Behavior ---
    "script.interval": ("int", 5, False, "Seconds between presence polls"),
    "script.localhost.port": ("int", 8969, False, "Local port used to catch the OAuth redirect"),

    # --- Reconnect ---
    "script.reconnect.enabled": ("bool", True, False, "Automatically reconnect (with RESUME when possible) if the Gateway connection drops"),
    "script.reconnect.base_delay": ("int", 5, False, "Seconds to wait before the first reconnect attempt; doubles after each further failure"),
    "script.reconnect.max_delay": ("int", 300, False, "Cap on the backoff delay between reconnect attempts, in seconds"),
    "script.reconnect.max_attempts": ("int", 0, False, "Give up after this many consecutive failed attempts; 0 means retry forever"),

    # --- Human notifications (clean, readable status updates -- NOT the dev log dump) ---
    "human.discord.webhook": ("list", [], False, "Discord webhook URL(s) for readable status updates, e.g. [\"url1\",\"url2\"]. Sent once per real status change, never a raw log dump. Multiple URLs are round-robined so no single webhook takes all the traffic."),
    "human.message.offline": ("str", "{user.display} went offline.", False, ""),
    "human.message.online": ("str", "{user.display} is online.", False, ""),
    "human.message.studio": ("str", "{user.display} is in Roblox Studio.", False, ""),
    "human.message.ingame": ("str", "{user.display} started playing {game.name}.", False, "Counts aren't included here by default -- add {game.server.current}/{game.server.max} yourself if you want them (blank when unavailable/private, same as everywhere else)"),
    "human.message.ingame.anonymous": ("str", "{user.display} started playing a game.", False, "Used instead of human.message.ingame when privacy.anonymous is on -- deliberately has no {game.name}"),

    # --- Dev / logging ---
    "script.dev.debug": ("bool", False, False, "Show DBG-level logs"),
    "script.dev.info": ("bool", False, False, "Show INF-level logs"),
    "script.dev.warn": ("bool", True, False, "Show WRN-level logs"),
    "script.dev.error": ("bool", True, False, "Show ERR-level logs"),
    "script.dev.discord.webhook": ("list", [], False, "Discord webhook URL(s) for raw batched log output, e.g. [\"url1\",\"url2\"]. This is the technical firehose -- see human.discord.webhook above for a readable alternative. Multiple URLs are round-robined."),
    "script.dev.discord.webhook.interval": ("int", 30, False, "Seconds between webhook log flushes"),
    "script.dev.alias": ("str", "ropysence", False, "Used as the Gateway client name and webhook embed author"),
    "script.dev.img.default": ("str", "https://raw.githubusercontent.com/codedcorerblx/ropysence/main/icon.png", False, "Fallback image for Online/Studio/Offline/anonymous-mode. A URL here is proxied automatically like any other image (recommended, zero setup); a bare string is instead treated as a literal Rich Presence Art Asset key you've manually uploaded in the dev portal. Leave blank to omit the image entirely."),
}

_TYPE_ORDER = ["Required", "Privacy", "Buttons", "Activity text", "Behavior", "Reconnect", "Human notifications", "Dev / logging"]
_SECTION_OF = {
    "script.user.id": "Required", "script.bot.id": "Required",
    "privacy.player.count": "Privacy", "privacy.player.name.placeholder": "Privacy", "privacy.anonymous": "Privacy",
    "rpc.button.one.text": "Buttons", "rpc.button.one.url": "Buttons",
    "rpc.button.two.text": "Buttons", "rpc.button.two.url": "Buttons",
    "rpc.game.name": "Activity text", "rpc.game.details": "Activity text", "rpc.game.details.unmatched": "Activity text",
    "rpc.game.details.anonymous": "Activity text", "rpc.game.details.online": "Activity text",
    "rpc.game.details.studio": "Activity text", "rpc.game.details.offline": "Activity text",
    "rpc.game.state": "Activity text", "rpc.type": "Activity text", "rpc.state": "Activity text",
    "script.interval": "Behavior", "script.localhost.port": "Behavior",
    "script.reconnect.enabled": "Reconnect", "script.reconnect.base_delay": "Reconnect",
    "script.reconnect.max_delay": "Reconnect", "script.reconnect.max_attempts": "Reconnect",
    "human.discord.webhook": "Human notifications", "human.message.offline": "Human notifications",
    "human.message.online": "Human notifications", "human.message.studio": "Human notifications",
    "human.message.ingame": "Human notifications", "human.message.ingame.anonymous": "Human notifications",
    "script.dev.debug": "Dev / logging", "script.dev.info": "Dev / logging", "script.dev.warn": "Dev / logging",
    "script.dev.error": "Dev / logging", "script.dev.discord.webhook": "Dev / logging",
    "script.dev.discord.webhook.interval": "Dev / logging", "script.dev.alias": "Dev / logging",
    "script.dev.img.default": "Dev / logging",
}

_VALID_RPC_STATES = {"online", "idle", "dnd"}
_VALID_RPC_TYPES = {0, 1, 2, 3, 5}


def render_default_template() -> str:
    lines = ["# " + l for l in PLACEHOLDER_HELP.splitlines()]
    lines.append("")
    for section in _TYPE_ORDER:
        lines.append(f"# --- {section} ---")
        for key, (_, default, required, comment) in OPTION_SCHEMA.items():
            if _SECTION_OF[key] != section:
                continue
            if comment:
                lines.append(f"# {comment}")
            if key == "script.user.id":
                lines.append('script.user.id="YOUR_DISCORD_APPLICATION_ID"')
            elif isinstance(default, bool):
                lines.append(f"{key}={'true' if default else 'false'}")
            elif isinstance(default, int):
                lines.append(f"{key}={default}")
            elif isinstance(default, list):
                lines.append(f"{key}={json.dumps(default)}")
            else:
                lines.append(f'{key}="{default}"')
        lines.append("")

    lines.append("# --- Custom placeholders (optional, unlimited, name them anything) ---")
    lines.append("# placeholder.<name>=\"value\"  -- reference as {custom.<name>} anywhere above.")
    lines.append("# Uncomment/edit the examples below, or add your own lines following the")
    lines.append("# same pattern.")
    lines.append('# placeholder.my.website="example.com"')
    lines.append('# rpc.button.two.url="https://{custom.my.website}"')
    lines.append("")
    return "\n".join(lines)


def _strip_quotes(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        return raw[1:-1]
    return raw


def _coerce(key: str, raw: str, expected_type: str):
    raw = raw.strip()
    if expected_type == "bool":
        low = _strip_quotes(raw).lower()
        if low not in ("true", "false"):
            raise ValueError(f"'{key}' expects true/false, got: {raw}")
        return low == "true"
    if expected_type == "int":
        try:
            return int(_strip_quotes(raw))
        except ValueError:
            raise ValueError(f"'{key}' expects an integer, got: {raw}")
    if expected_type == "list":
        if raw.startswith("["):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                raise ValueError(f"'{key}' expects a JSON array like [\"url1\",\"url2\"], got: {raw}")
            if not isinstance(parsed, list):
                raise ValueError(f"'{key}' expects a JSON array like [\"url1\",\"url2\"], got: {raw}")
            return [str(v).strip() for v in parsed if str(v).strip()]
        # convenience/back-compat: a single bare or quoted URL
        single = _strip_quotes(raw)
        return [single] if single else []
    # str
    return _strip_quotes(raw)


def _parse_lines(text: str) -> dict:
    raw = {}
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            log.warning("options.txt line %d looks malformed (no '='), ignoring: %s", lineno, stripped)
            continue
        key, _, value = stripped.partition("=")
        raw[key.strip()] = value
    return raw


def load_options() -> dict:
    APP_DIR.mkdir(parents=True, exist_ok=True)

    if not OPTIONS_FILE.exists():
        log.info("no options.txt found, writing a documented default to %s", OPTIONS_FILE)
        OPTIONS_FILE.write_text(render_default_template())
        log.error("options.txt was just created -- set script.user.id to your Discord Application ID, then rerun")
        raise SystemExit(1)

    raw = _parse_lines(OPTIONS_FILE.read_text())

    custom_placeholders = {}
    for key in list(raw.keys()):
        if key.startswith(CUSTOM_PLACEHOLDER_PREFIX):
            name = key[len(CUSTOM_PLACEHOLDER_PREFIX):]
            if not name:
                log.warning("options.txt has a bare 'placeholder.' with no name after it -- ignoring")
                del raw[key]
                continue
            custom_placeholders[name] = _strip_quotes(raw.pop(key))

    for key in raw:
        if key not in OPTION_SCHEMA:
            log.warning("unknown option '%s' in options.txt -- ignoring", key)

    resolved = {}
    missing_required = []
    for key, (typ, default, required, _comment) in OPTION_SCHEMA.items():
        if key in raw:
            try:
                resolved[key] = _coerce(key, raw[key], typ)
            except ValueError as e:
                log.error("options.txt: %s -- using default instead", e)
                resolved[key] = default
        else:
            resolved[key] = default

        if required and (resolved[key] == "" or resolved[key] is None):
            missing_required.append(key)

    if missing_required:
        for key in missing_required:
            log.error("required option '%s' is not set in options.txt", key)
        raise SystemExit(1)

    if resolved["rpc.state"].lower() not in _VALID_RPC_STATES:
        log.error(
            "rpc.state='%s' is not valid -- must be one of %s (invisible is not supported for this session type)",
            resolved["rpc.state"], sorted(_VALID_RPC_STATES),
        )
        raise SystemExit(1)
    resolved["rpc.state"] = resolved["rpc.state"].lower()

    if resolved["rpc.type"] not in _VALID_RPC_TYPES:
        log.error("rpc.type=%s is not valid -- must be one of %s", resolved["rpc.type"], sorted(_VALID_RPC_TYPES))
        raise SystemExit(1)

    img_default = resolved["script.dev.img.default"]
    if img_default and not img_default.startswith(("http://", "https://")):
        log.warning(
            "script.dev.img.default='%s' is not a URL, so it's being treated as a literal Rich "
            "Presence Art Asset key -- this ONLY works if you've manually uploaded an image under "
            "that exact name in your Discord app's dev portal already. If you haven't, or if this "
            "is a leftover value from an older version of options.txt, referencing a nonexistent "
            "asset key appears to make Discord silently drop the entire activity update, not just "
            "the image. Safer options: point it at a URL (auto-proxied, no setup), or clear it to "
            "omit the image.", img_default,
        )

    if resolved["human.discord.webhook"] and resolved["human.discord.webhook"] == resolved["script.dev.discord.webhook"]:
        log.warning(
            "human.discord.webhook and script.dev.discord.webhook point at the exact same URL(s) -- "
            "you'll get both the readable status updates AND the raw technical log dump in the same "
            "channel. That's allowed, just flagging it in case it wasn't intentional."
        )

    if custom_placeholders:
        log.debug("parsed %d custom placeholder(s): %s", len(custom_placeholders), sorted(custom_placeholders))
    resolved["_custom_placeholders"] = custom_placeholders

    log.debug("options.txt loaded (%d keys, %d custom placeholder(s))", len(resolved), len(custom_placeholders))
    return resolved
