"""
Thin wrapper around ThreadPoolExecutor for dispatching independent, I/O-bound
API calls (Roblox HTTP requests) concurrently.

Threads, not subprocesses/multiprocessing, on purpose: these tasks spend
almost all their time waiting on network I/O, during which Python releases
the GIL anyway -- so threads give the real concurrency benefit here without
the process-spawn overhead multiprocessing would add for no gain.
"""

from concurrent.futures import ThreadPoolExecutor, Future

from src.core.logging_setup import get_logger
from src.workers.timing import timed

log = get_logger("worker_pool")


class WorkerPool:
    def __init__(self, max_workers: int = 4, thread_name_prefix: str = "worker"):
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix=thread_name_prefix)
        log.info("worker pool started (max_workers=%d, prefix=%s)", max_workers, thread_name_prefix)

    def submit(self, label: str, fn, *args, **kwargs) -> Future:
        """Submit fn(*args, **kwargs) to the pool, auto-wrapped with timing
        under the given label (shows up in DBG logs as '[label] ...')."""
        return self._executor.submit(timed(label)(fn), *args, **kwargs)

    def shutdown(self) -> None:
        log.debug("shutting down worker pool")
        self._executor.shutdown(wait=False, cancel_futures=True)
