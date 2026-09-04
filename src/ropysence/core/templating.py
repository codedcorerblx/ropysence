"""
{token} templating for options.txt content fields, plus support for
user-defined custom placeholders (placeholder.<name>="value" in options.txt,
referenced as {custom.<name>} anywhere).

Deliberately not using str.format(): dotted tokens like {game.name} trigger
attribute-access semantics in str.format, not dict-key lookup, which fights
a flat dotted-key context dict. A small regex substitution is simpler.
"""

import re

from src.ropysence.core.logging_setup import get_logger

log = get_logger("templating")

_TOKEN_RE = re.compile(r"\{([a-zA-Z0-9_]+(?:\.[a-zA-Z0-9_]+)*)\}")


def render(template: str, context: dict) -> str:
    """context is flat, keyed by dotted strings, e.g. {"game.name": "Bloxburg"}.
    Missing tokens (or explicitly None values, used to blank a token out for
    privacy reasons) render as empty string -- expected/normal."""
    return render_track_missing(template, context)[0]


def render_track_missing(template: str, context: dict) -> tuple:
    """Same as render(), but also returns whether any token in the template
    was missing/None. Used to decide whether a fully user-configurable
    button should be shown at all -- e.g. the default Join button's URL
    references {game.id}/{game.instance}, which are only populated while
    actually in a game; outside that, the button is skipped rather than
    shown with a broken link. This generalizes to any custom button: if a
    placeholder it depends on isn't available right now, it's hidden."""
    if not template:
        return "", False

    missing = [False]

    def _sub(match: re.Match) -> str:
        token = match.group(1)
        value = context.get(token)
        if value is None:
            missing[0] = True
            log.debug("template token '{%s}' not present in this context, rendering empty", token)
            return ""
        return str(value)

    return _TOKEN_RE.sub(_sub, template), missing[0]


def resolve_custom_placeholders(raw_templates: dict, base_context: dict, max_passes: int = 5) -> dict:
    """raw_templates: {name: raw_template_string} as parsed from
    `placeholder.<name>="..."` lines in options.txt (name has NOT been
    prefixed with "custom." yet). Returns {"custom.<name>": resolved_value}
    ready to merge into a render context.

    Custom placeholder values may themselves reference other placeholders --
    built-in ones (which change every poll cycle, hence this being resolved
    fresh per build() call rather than once at options-load time) or other
    custom ones, in any declaration order. Resolved with a few fixed-point
    passes so forward and backward references both work; a value still
    containing an unresolved token after max_passes is logged (likely a
    circular reference, or it points at something that doesn't exist)."""
    if not raw_templates:
        return {}

    working = dict(base_context)
    for name in raw_templates:
        working.setdefault(f"custom.{name}", None)

    for _pass in range(max_passes):
        changed = False
        for name, template in raw_templates.items():
            key = f"custom.{name}"
            new_value = render(template, working)
            if working.get(key) != new_value:
                working[key] = new_value
                changed = True
        if not changed:
            break

    result = {}
    for name in raw_templates:
        key = f"custom.{name}"
        value = working.get(key, "")
        if _TOKEN_RE.search(value or ""):
            log.warning(
                "custom placeholder '{%s}' still contains an unresolved token after %d pass(es) "
                "-- likely a circular reference, or it points at something that doesn't exist. Got: %r",
                key, max_passes, value,
            )
        result[key] = value
    return result


def available_tokens() -> list:
    return [
        "user.name", "user.display", "user.id",
        "game.name", "game.id", "game.instance",
        "game.server.current", "game.server.min", "game.server.max",
        "custom.<name> (from your own placeholder.<name>=... lines)",
    ]
