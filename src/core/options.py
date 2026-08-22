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

Lines starting with # (after stripping leading whitespace) and blank lines
are ignored. Unknown keys are warned about, not fatal, so a stray typo
doesn't crash the whole config.
"""

import re
from pathlib import Path

from src.core.logging_setup import get_logger
from src.core.secure_store import APP_DIR

log = get_logger("options")

OPTIONS_FILE = APP_DIR / "options.txt"

PLACEHOLDER_HELP = (
    "Supported placeholders (usable inside any \"quoted\" content field):\n"
    "  {user.name}            Roblox username\n"
    "  {user.display}         Roblox display name\n"
    "  {game.name}            Current game name\n"
    "  {game.server.current}  Players currently on the matched server\n"
    "  {game.server.min}      Server's minimum player count (Roblox rarely exposes this -- often blank)\n"
    "  {game.server.max}      Server's maximum player count\n"
    "Missing placeholders render as empty text, never an error."
)

# key -> (type, default, required, comment)
# type is one of "bool", "int", "str"
OPTION_SCHEMA = {
    # --- Required ---
    "script.user.id": ("str", "", True, "Discord Application (Client) ID -- required"),
    "script.bot.id": ("str", "", False, "Reserved for future use -- not consumed by the current build, safe to leave blank"),

    # --- Privacy ---
    "privacy.player.count": ("bool", True, False, "Show current/max player counts for the matched server"),
    "privacy.player.name.placeholder": ("bool", False, False, "When counts can't be fetched, show your username in the fallback text instead of a blank"),
    "privacy.anonymous": ("bool", False, False, "Hide game name/counts/buttons entirely; large image becomes script.dev.img.default"),

    # --- Buttons (Discord allows max 2; join takes slot 1, profile/gamepage compete for slot 2 -- profile wins if both are on) ---
    "rpc.button.join": ("bool", True, False, "Show a 'Join Game' button when actually in a joinable game"),
    "rpc.button.profile": ("bool", True, False, "Show a profile-link button"),
    "rpc.button.gamepage": ("bool", False, False, "Show a game-page-link button (only used if profile is off)"),
    "rpc.button.join.content": ("str", "Join Game", False, ""),
    "rpc.button.profile.content": ("str", "{user.name}'s Profile", False, ""),
    "rpc.button.gamepage.content": ("str", "See Game Page", False, ""),

    # --- Activity text ---
    "rpc.game.name": ("str", "Roblox", False, "Activity name shown at the top"),
    "rpc.game.details": ("str", "In Game ({game.server.current}/{game.server.max})", False, "Used when in a game AND a public server match with counts was found"),
    "rpc.game.details.unmatched": ("str", "In Game ({user.name})", False, "Used when in a game but no count is available (respects privacy.player.name.placeholder)"),
    "rpc.game.details.anonymous": ("str", "In Game", False, "Used when in a game AND privacy.anonymous is on"),
    "rpc.game.details.online": ("str", "Online", False, ""),
    "rpc.game.details.studio": ("str", "In Studio", False, ""),
    "rpc.game.details.offline": ("str", "Offline", False, ""),
    "rpc.game.state": ("str", "{game.name}", False, "Only used while in a game"),
    "rpc.type": ("int", 0, False, "Discord activity type: 0 Playing, 1 Streaming, 2 Listening, 3 Watching, 5 Competing"),
    "rpc.state": ("str", "online", False, "Discord status shown alongside the activity: online / idle / dnd (invisible is not supported)"),

    # --- Behavior ---
    "script.interval": ("int", 5, False, "Seconds between presence polls"),
    "script.localhost.port": ("int", 8969, False, "Local port used to catch the OAuth redirect"),

    # --- Dev / logging ---
    "script.dev.debug": ("bool", False, False, "Show DBG-level logs"),
    "script.dev.info": ("bool", False, False, "Show INF-level logs"),
    "script.dev.warn": ("bool", True, False, "Show WRN-level logs"),
    "script.dev.error": ("bool", True, False, "Show ERR-level logs"),
    "script.dev.discord.webhook": ("str", "", False, "Discord webhook URL for remote logging, sent in periodic batches"),
    "script.dev.discord.webhook.interval": ("int", 30, False, "Seconds between webhook log flushes"),
    "script.dev.alias": ("str", "ropysence", False, "Used as the Gateway client name and webhook embed author"),
    "script.dev.img.default": ("str", "https://raw.githubusercontent.com/codedcorerblx/ropysence/main/icon.png", False, "Fallback image for Online/Studio/Offline/anonymous-mode. A URL here is proxied automatically like any other image (recommended, zero setup); a bare string is instead treated as a literal Rich Presence Art Asset key you've manually uploaded in the dev portal. Leave blank to omit the image entirely."),
}

_TYPE_ORDER = ["Required", "Privacy", "Buttons", "Activity text", "Behavior", "Dev / logging"]
_SECTION_OF = {
    "script.user.id": "Required", "script.bot.id": "Required",
    "privacy.player.count": "Privacy", "privacy.player.name.placeholder": "Privacy", "privacy.anonymous": "Privacy",
    "rpc.button.join": "Buttons", "rpc.button.profile": "Buttons", "rpc.button.gamepage": "Buttons",
    "rpc.button.join.content": "Buttons", "rpc.button.profile.content": "Buttons", "rpc.button.gamepage.content": "Buttons",
    "rpc.game.name": "Activity text", "rpc.game.details": "Activity text", "rpc.game.details.unmatched": "Activity text",
    "rpc.game.details.anonymous": "Activity text", "rpc.game.details.online": "Activity text",
    "rpc.game.details.studio": "Activity text", "rpc.game.details.offline": "Activity text",
    "rpc.game.state": "Activity text", "rpc.type": "Activity text", "rpc.state": "Activity text",
    "script.interval": "Behavior", "script.localhost.port": "Behavior",
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
            else:
                lines.append(f'{key}="{default}"')
        lines.append("")
    return "\n".join(lines)


def _coerce(key: str, raw: str, expected_type: str):
    raw = raw.strip()
    if expected_type == "bool":
        low = raw.strip('"').lower()
        if low not in ("true", "false"):
            raise ValueError(f"'{key}' expects true/false, got: {raw}")
        return low == "true"
    if expected_type == "int":
        try:
            return int(raw.strip('"'))
        except ValueError:
            raise ValueError(f"'{key}' expects an integer, got: {raw}")
    # str
    if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        return raw[1:-1]
    return raw


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

    log.debug("options.txt loaded (%d keys)", len(resolved))
    return resolved
