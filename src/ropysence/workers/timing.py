"""
Tiny timing helper so parallel worker tasks can report how long they took --
useful both for perf visibility and for narrowing down which specific API
call is slow or misbehaving.
"""

import functools
import time

from src.ropysence.core.logging_setup import get_logger

log = get_logger("timing")


def timed(label: str):
    """Decorator: logs DBG start/end + elapsed ms around a worker function.
    Exceptions are logged at WRN (with elapsed time) and re-raised untouched
    so calling code's own error handling still applies."""
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            start = time.perf_counter()
            log.debug("[%s] worker task starting", label)
            try:
                result = fn(*args, **kwargs)
            except Exception:
                elapsed_ms = (time.perf_counter() - start) * 1000
                log.warning("[%s] worker task raised after %.1fms", label, elapsed_ms)
                raise
            elapsed_ms = (time.perf_counter() - start) * 1000
            log.debug("[%s] worker task finished in %.1fms", label, elapsed_ms)
            return result
        return wrapper
    return decorator
