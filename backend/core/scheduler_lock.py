"""Single-replica scheduler leadership via Redis SET NX.

When Redis is unavailable, the caller may still run jobs (single-instance
assumption). Extra web replicas should set ENABLE_SCHEDULER=false if Redis
is not configured.
"""
from __future__ import annotations

import logging
import os
import threading

from core.redis import redis_cache

logger = logging.getLogger(__name__)

LOCK_KEY = "lme:scheduler:leader"
LOCK_TTL_SECONDS = 120
REFRESH_EVERY_SECONDS = 45

_holder = False
_stop = threading.Event()
_refresh_thread: threading.Thread | None = None


def acquire_scheduler_leadership() -> bool:
    """Return True if this process should run in-process cron jobs."""
    global _holder
    if not redis_cache.enabled:
        logger.info(
            "Redis disabled — starting scheduler without a leader lock "
            "(set ENABLE_SCHEDULER=false on extra replicas)"
        )
        _holder = True
        return True

    token = str(os.getpid())
    acquired = redis_cache.set(LOCK_KEY, token, ex=LOCK_TTL_SECONDS, nx=True)
    _holder = bool(acquired)
    if _holder:
        logger.info("Acquired scheduler leader lock (%s)", LOCK_KEY)
    else:
        logger.info("Scheduler leader lock already held — skipping cron jobs on this replica")
    return _holder


def start_scheduler_lock_refresh() -> None:
    """Keep the Redis lock alive while this process remains the leader."""
    global _refresh_thread
    if not redis_cache.enabled or not _holder:
        return
    if _refresh_thread and _refresh_thread.is_alive():
        return
    _stop.clear()

    def _loop() -> None:
        while not _stop.wait(REFRESH_EVERY_SECONDS):
            try:
                redis_cache.set(LOCK_KEY, str(os.getpid()), ex=LOCK_TTL_SECONDS, nx=False)
            except Exception:
                logger.exception("Failed to refresh scheduler leader lock")

    _refresh_thread = threading.Thread(
        target=_loop, daemon=True, name="scheduler-lock-refresh"
    )
    _refresh_thread.start()


def release_scheduler_leadership() -> None:
    global _holder
    _stop.set()
    if _holder and redis_cache.enabled:
        redis_cache.delete(LOCK_KEY)
    _holder = False
