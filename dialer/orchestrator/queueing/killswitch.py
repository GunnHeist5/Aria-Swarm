"""Global kill switch: one command halts all dialing immediately (SPEC §8).

Two stores, two jobs: Redis is the fast path every dial consults; `kv_state`
in Postgres is the authority that survives restarts and Redis flushes. The
asymmetry is deliberate:

- ENGAGE must succeed if *any* store can record it (an emergency stop that
  refuses to stop is the worst failure mode). It raises only when both fail.
- RELEASE must land in Postgres (the authority) or it raises — a release we
  cannot persist did not happen.
- IS_ENGAGED fails closed: a Redis error counts as engaged unless kv_state
  explicitly says "released". A flushed Redis with kv saying "engaged" is
  still engaged. If neither store can be consulted, dialing is off.
"""

from __future__ import annotations

from datetime import datetime, timezone

import redis

from .. import db
from ..config import Settings
from ..logging_utils import get_logger

log = get_logger(__name__)

REDIS_KEY = "killswitch:engaged"
KV_KEY = "killswitch"

# One client per redis URL, built lazily (tests monkeypatch this factory).
_clients: dict[str, "redis.Redis"] = {}


def _redis(cfg: Settings) -> "redis.Redis":
    client = _clients.get(cfg.redis_url)
    if client is None:
        client = redis.Redis.from_url(cfg.redis_url)
        _clients[cfg.redis_url] = client
    return client


def engage(cfg: Settings, reason: str) -> None:
    """Halt all dialing. Records to both stores; raises only if BOTH fail."""
    stamp = {
        "engaged": True,
        "reason": reason,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    redis_ok = kv_ok = False
    try:
        _redis(cfg).set(REDIS_KEY, reason or "engaged")
        redis_ok = True
    except Exception:
        log.exception("kill switch: Redis write failed")
    try:
        db.kv_set(cfg, KV_KEY, stamp)
        kv_ok = True
    except Exception:
        log.exception("kill switch: kv_state write failed")
    if not (redis_ok or kv_ok):
        raise RuntimeError(
            "kill switch could not be recorded in Redis OR Postgres — dialing may continue"
        )
    if not kv_ok:
        # Redis-only engagement does not survive a flush; the operator must know.
        log.error("kill switch engaged in Redis ONLY — will not survive a Redis flush")
    log.warning("KILL SWITCH ENGAGED (reason=%s)", reason)


def release(cfg: Settings) -> None:
    """Re-enable dialing. The authoritative (kv_state) write must succeed."""
    db.kv_set(
        cfg,
        KV_KEY,
        {"engaged": False, "released_at": datetime.now(timezone.utc).isoformat()},
    )
    try:
        _redis(cfg).delete(REDIS_KEY)
    except Exception:
        # The stale Redis flag keeps dialing halted until Redis recovers —
        # wrong direction for the operator, safe direction for compliance.
        log.exception(
            "kill switch released in Postgres but the Redis flag remains — "
            "dialing stays halted until Redis recovers"
        )
    log.warning("kill switch released")


def is_engaged(cfg: Settings) -> bool:
    """True when dialing must halt. Fails closed on any doubt."""
    redis_error = False
    try:
        if _redis(cfg).get(REDIS_KEY) is not None:
            return True
    except Exception:
        log.exception("kill switch: Redis unreadable — consulting kv_state")
        redis_error = True

    # Redis says "not engaged" or is unavailable — Postgres is the authority.
    try:
        state = db.kv_get(cfg, KV_KEY)
    except Exception:
        log.exception("kill switch: kv_state unreadable — treating as ENGAGED")
        return True
    if state is None:
        # Never engaged (fresh system). With Redis down we cannot rule out an
        # engage that only reached Redis, so fail closed.
        return redis_error
    return bool(state.get("engaged"))
