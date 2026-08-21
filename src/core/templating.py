"""
Minimal {token} templating for options.txt content fields.

Deliberately not using str.format(): dotted tokens like {game.name} trigger
attribute-access semantics in str.format, not dict-key lookup, which fights
a flat dotted-key context dict. A small regex substitution is simpler.
"""

import re

from src.core.logging_setup import get_logger

log = get_logger("templating")

_TOKEN_RE = re.compile(r"\{([a-zA-Z0-9_]+(?:\.[a-zA-Z0-9_]+)*)\}")


def render(template: str, context: dict) -> str:
    """context is flat, keyed by dotted strings, e.g. {"game.name": "Bloxburg"}.
    Missing tokens (or explicitly None values, used to blank a token out for
    privacy reasons) render as empty string -- expected/normal."""
    if not template:
        return ""

    def _sub(match: re.Match) -> str:
        token = match.group(1)
        value = context.get(token)
        if value is None:
            log.debug("template token '{%s}' not present in this context, rendering empty", token)
            return ""
        return str(value)

    return _TOKEN_RE.sub(_sub, template)


def available_tokens() -> list:
    return [
        "user.name", "user.display",
        "game.name", "game.server.current", "game.server.min", "game.server.max",
    ]
