"""
One shared fixed-window rate limiter: Redis when configured, in-process when not.

Why this module exists: three surfaces grew three private copies of the same
deque-in-a-dict limiter (the customer page, the voice webhook, the Plivo
bridge), each correct ONLY at WEB_CONCURRENCY=1 — render.yaml pins 1 and its
comment says why: N workers silently multiply every per-IP limit N times,
which turns a safety bound into an advisory. The fix is not "remember to
keep one worker forever"; it is making the limit shared across workers.

REDIS_URL set  -> INCR + EXPIRE, atomic across every replica (~0.1ms).
REDIS_URL unset -> the exact in-process deque the call sites had before,
                   so dev, the demo and tests need no Redis. Fail-open to the
                   OLD limiter, never to "no limiter" — an unreachable Redis
                   at runtime degrades to per-process counting rather than
                   an outage on a public page.

The GC guard (drop stale buckets past 10k keys) exists because the map keys
on client IP: a slow drip from many addresses grows it without bound, and
behind India's carrier-grade NAT one shared IP can represent thousands of
legitimate customers — the map must not be the thing that fills.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import Any

from src.config import get_settings

logger = logging.getLogger(__name__)

# Same shape every call site already used: 60s window, GC past 10k keys.
DEFAULT_WINDOW_SECONDS = 60.0
_GC_AT = 10_000

# In-process fallback state — shared module-level, not per-call-site, so the
# three surfaces share one GC pass instead of three.
_BUCKETS: dict[str, deque[float]] = {}

# A process-global Redis client pool, built lazily once. redis-py's client is
# thread/task-safe and its commands are atomic; building it per call would
# open a connection storm under exactly the load this module exists for.
_REDIS: Any = None
_REDIS_TRIED = False


def _redis() -> Any:
    """The shared client, or None when REDIS_URL is unset or unreachable."""
    global _REDIS, _REDIS_TRIED
    if not _REDIS_TRIED:
        _REDIS_TRIED = True
        url = get_settings().redis_url
        if url:
            try:
                import redis  # type: ignore[import-not-found]

                _REDIS = redis.Redis.from_url(
                    url, socket_timeout=0.5, socket_connect_timeout=1.0,
                    decode_responses=True,
                )
                _REDIS.ping()
                logger.info("Rate limiting on Redis: shared across workers")
            except Exception:
                # Fail-open to the in-process limiter and say so loudly: a
                # limiter that 500s the page it guards is worse than one that
                # under-counts across workers.
                logger.exception(
                    "REDIS_URL is set but unreachable — rate limits fall back "
                    "to per-process and are NOT shared across workers"
                )
                _REDIS = None
    return _REDIS


def _now() -> float:
    return time.monotonic()


def check(key: str, limit: int, *, window: float = DEFAULT_WINDOW_SECONDS) -> None:
    """
    Consume one slot for `key` or raise HTTPException 429.

    Raises fastapi.HTTPException directly — every call site is a FastAPI
    route handler, and returning a sentinel they must all re-raise just
    spreads the same three lines to three places again.
    """
    from fastapi import HTTPException

    r = _redis()
    if r is not None:
        try:
            redis_key = f"rl:{key}"
            # SET NX EX first: the counter is created WITH its expiry in one
            # atomic step, so no crash-window can ever leave an immortal
            # counter (INCR-then-EXPIRE has exactly that race — the process
            # dying between them leaves a key with no TTL that never resets
            # and, at CGNAT-shared IPs, locks out real customers forever).
            created = r.set(redis_key, 1, nx=True, ex=max(1, int(window)))
            if created:
                return
            count = r.incr(redis_key)
            # TTL backstop: an old key created by a pre-fix deployment (or a
            # SETNX that raced a manual FLUSH) has no expiry — restore one
            # lazily rather than letting the counter live forever.
            if r.ttl(redis_key) < 0:
                r.expire(redis_key, max(1, int(window)))
            if count > limit:
                logger.warning("Rate limit hit (redis): %s (%d in window)", key, count)
                raise HTTPException(status_code=429, detail="Too many requests")
            return
        except HTTPException:
            raise
        except Exception:
            # Redis died BETWEEN requests — degrade, don't drop the page.
            logger.warning("Redis rate check failed — in-process fallback")

    now_mono = _now()
    bucket = _BUCKETS.setdefault(key, deque())
    while bucket and now_mono - bucket[0] > window:
        bucket.popleft()
    if len(bucket) >= limit:
        logger.warning("Rate limit hit: %s (%d in window)", key, len(bucket))
        raise HTTPException(status_code=429, detail="Too many requests")
    bucket.append(now_mono)
    if len(_BUCKETS) > _GC_AT:
        stale = [
            k for k, v in _BUCKETS.items()
            if not v or now_mono - v[-1] > window
        ]
        for stale_key in stale:
            del _BUCKETS[stale_key]


def clear_all() -> None:
    """Test hook: reset both stores so suite runs can't contaminate each other."""
    global _REDIS, _REDIS_TRIED
    _BUCKETS.clear()
    _REDIS = None
    _REDIS_TRIED = False
