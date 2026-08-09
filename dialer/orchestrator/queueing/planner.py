"""Planner: audience rows → `scheduled` call_attempts.

The planner is the only writer of NEW first attempts (retries and second-touch
webhooks add their own rows through their own paths). It runs the full
compliance gate per contact even though the dial worker re-checks moments
before dialing — planning is where permanent blocks get tallied for
observability (SPEC §9) and where temporal blocks pick their retry moment, so
the worker's re-check is a backstop, not the primary filter.

Scheduling spreads `scheduled_for` so no minute holds more than
`calls_per_minute` attempts; the Redis limiter then enforces the same budget
at dial time against reality (retries, webhooks, restarts).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .. import db
from ..config import Settings
from ..logging_utils import get_logger
from ..models import TouchType

log = get_logger(__name__)

_PROGRESS_FOR_TOUCH = {TouchType.FIRST: "undialed", TouchType.SECOND: "dialed"}

_LIVE_STATUSES = "('scheduled', 'queued', 'dialing', 'in_progress')"

# LIMIT NULL is Postgres for "no limit", so one statement serves both cases.
_AUDIENCE_SQL = f"""
SELECT c.*
FROM contacts c
WHERE c.touch_type = %(touch)s
  AND lower(coalesce(c.progress_status, '')) = %(progress)s
  AND NOT EXISTS (
        SELECT 1 FROM call_attempts a
        WHERE a.contact_id = c.id AND a.status IN {_LIVE_STATUSES}
  )
  AND (SELECT count(*) FROM call_attempts a
       WHERE a.contact_id = c.id AND a.status <> 'canceled') < %(cap)s
ORDER BY c.id
LIMIT %(limit)s
"""

_NEXT_ATTEMPT_SQL = """
SELECT COALESCE(MAX(attempt_number), 0) + 1 AS n
FROM call_attempts
WHERE contact_id = %(contact_id)s AND touch_type = %(touch)s
"""

_LIVE_SECOND_TOUCH_SQL = f"""
SELECT count(*) AS n FROM call_attempts
WHERE touch_type = 'second' AND status IN {_LIVE_STATUSES}
"""

# ON CONFLICT DO NOTHING: a concurrent planner/webhook that won the race keeps
# its row; we simply do not count the insert.
_INSERT_SQL = """
INSERT INTO call_attempts
    (idempotency_key, contact_id, touch_type, attempt_number, status,
     to_phone_e164, opener_variant, consent_basis, scheduled_for)
VALUES (%(key)s, %(contact_id)s, %(touch)s, %(n)s, 'scheduled',
        %(phone)s, %(opener)s, %(consent)s, %(when)s)
ON CONFLICT (idempotency_key) DO NOTHING
"""


@dataclass
class PlanStats:
    considered: int
    scheduled: int
    blocked: dict[str, int]


class _MinuteSpreader:
    """Assign each attempt the earliest minute (≥ its base time) with room."""

    def __init__(self, calls_per_minute: int):
        self._cpm = max(calls_per_minute, 1)
        self._counts: Counter[datetime] = Counter()

    def slot(self, base: datetime) -> datetime:
        minute = base.replace(second=0, microsecond=0)
        while self._counts[minute] >= self._cpm:
            minute += timedelta(minutes=1)
        self._counts[minute] += 1
        return max(base, minute)


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def plan_touch(
    cfg: Settings,
    touch: TouchType,
    *,
    limit: int | None = None,
    now: datetime | None = None,
) -> PlanStats:
    """Plan one audience: gate every candidate, schedule the allowed ones."""
    # Lazy imports per the module contract: compliance and agent are sibling
    # packages; this module must import (and test) without them resolvable.
    from .. import compliance
    from ..agent import script

    now = _aware(now) if now is not None else datetime.now(timezone.utc)

    # Phase 2 is the small live slice (SPEC §12): first touch is refused
    # outright, and second touch is hard-clamped by live attempt count.
    if cfg.phase == 2 and touch == TouchType.FIRST:
        log.warning("phase 2: refusing to plan the FIRST-touch audience")
        return PlanStats(considered=0, scheduled=0, blocked={})
    effective_limit = limit
    if cfg.phase == 2 and touch == TouchType.SECOND:
        row = db.query_one(cfg, _LIVE_SECOND_TOUCH_SQL)
        live = int(row["n"]) if row else 0
        budget = max(cfg.phase2_max_contacts - live, 0)
        if budget == 0:
            log.info("phase 2: second-touch live attempts already at cap (%d)", live)
            return PlanStats(considered=0, scheduled=0, blocked={})
        effective_limit = budget if limit is None else min(limit, budget)

    contacts = db.query(
        cfg,
        _AUDIENCE_SQL,
        {
            "touch": touch.value,
            "progress": _PROGRESS_FOR_TOUCH[touch],
            "cap": cfg.max_attempts_total,
            "limit": effective_limit,
        },
    )

    spreader = _MinuteSpreader(cfg.calls_per_minute)
    blocked: Counter[str] = Counter()
    scheduled = 0
    for contact in contacts:
        decision = compliance.check_dial_allowed(cfg, contact, now=now)
        if decision.allowed:
            base = now  # allowed means the window is open right now
        elif decision.permanently_blocked or decision.earliest_allowed is None:
            # Permanent → never schedule. Temporal with no computable reopen
            # (kill switch, unknown zone) → skip too: planning a time we
            # cannot justify is a guess, and the next planner run will see it.
            for reason in decision.reasons:
                blocked[reason.value] += 1
            continue
        else:
            base = _aware(decision.earliest_allowed)

        row = db.query_one(
            cfg, _NEXT_ATTEMPT_SQL, {"contact_id": contact["id"], "touch": touch.value}
        )
        attempt_number = int(row["n"]) if row else 1
        inserted = db.execute(
            cfg,
            _INSERT_SQL,
            {
                "key": f"contact:{contact['id']}:touch:{touch.value}:attempt:{attempt_number}",
                "contact_id": contact["id"],
                "touch": touch.value,
                "n": attempt_number,
                "phone": contact["phone_e164"],
                "opener": script.pick_opener_variant(cfg, int(contact["id"])),
                # Snapshot for the audit trail — the gate already required it.
                "consent": contact.get("consent_basis"),
                "when": spreader.slot(base),
            },
        )
        if inserted:
            scheduled += 1

    stats = PlanStats(considered=len(contacts), scheduled=scheduled, blocked=dict(blocked))
    log.info(
        "planned %s touch: considered=%d scheduled=%d blocked=%s",
        touch.value, stats.considered, stats.scheduled, stats.blocked,
    )
    return stats
