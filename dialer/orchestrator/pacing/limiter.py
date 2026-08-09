"""Calls-per-minute limiter: a fixed-window counter in Redis.

A fixed window (INCR on `pacing:cpm:{unix_minute}`) is intentionally simple:
the worst burst error is 2× CPM across a minute boundary, which the per-number
caps and arq concurrency already absorb. The 120s expiry lets a key outlive
its minute so a slow worker never resurrects a dead window, while Redis still
garbage-collects it.

Fail closed: if Redis cannot be consulted, no slot is granted. A skipped
minute of dialing costs nothing; an unmetered minute can torch number
reputation.
"""

from __future__ import annotations

import time as _time

import redis

from ..config import Settings
from ..logging_utils import get_logger

log = get_logger(__name__)

_WINDOW_TTL_SECONDS = 120

# One client per redis URL, built lazily. redis-py clients own a thread-safe
# connection pool, so sharing across worker threads is fine.
_clients: dict[str, "redis.Redis"] = {}


def _redis(cfg: Settings) -> "redis.Redis":
    client = _clients.get(cfg.redis_url)
    if client is None:
        client = redis.Redis.from_url(cfg.redis_url)
        _clients[cfg.redis_url] = client
    return client


def _unix_minute() -> int:
    return int(_time.time() // 60)


def try_acquire_call_slot(cfg: Settings) -> bool:
    """Take one CPM token for the current minute; False = defer the dial."""
    key = f"pacing:cpm:{_unix_minute()}"
    try:
        client = _redis(cfg)
        count = int(client.incr(key))
        client.expire(key, _WINDOW_TTL_SECONDS)
    except Exception:
        log.exception("CPM limiter unavailable — refusing call slot (fail closed)")
        return False
    return count <= cfg.calls_per_minute
