"""Retry scheduling: no_answer / busy / failed → the next attempt, capped.

The retry schedule is positional: finishing attempt N waits
`retry_delays()[N-1]` before attempt N+1, so a 3-entry schedule allows at most
4 attempts — and `max_attempts_total` (counted per PHONE across both touch
types, via the compliance package) caps it again independently. Exhaustion of
either budget returns None: the contact simply stops being dialed.

The delay target is clamped forward into the prospect's next legal calling
window; a phone whose window cannot be computed (unknown zone) is not
rescheduled at all — guessing a time for a number we cannot place is exactly
the kind of convenience the compliance posture forbids.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Mapping

from .. import db
from ..config import Settings
from ..logging_utils import get_logger

log = get_logger(__name__)

_NEXT_ATTEMPT_SQL = """
SELECT COALESCE(MAX(attempt_number), 0) + 1 AS n
FROM call_attempts
WHERE contact_id = %(contact_id)s AND touch_type = %(touch)s
"""

# ON CONFLICT DO NOTHING + RETURNING: a lost race (duplicate webhook, crashed
# worker rerun) yields no row and we report "not scheduled" instead of dialing
# twice — the idempotency_key unique index is the real guarantee.
_INSERT_SQL = """
INSERT INTO call_attempts
    (idempotency_key, contact_id, touch_type, attempt_number, status,
     to_phone_e164, opener_variant, consent_basis, scheduled_for)
VALUES (%(key)s, %(contact_id)s, %(touch)s, %(n)s, 'scheduled',
        %(phone)s, %(opener)s, %(consent)s, %(when)s)
ON CONFLICT (idempotency_key) DO NOTHING
RETURNING id
"""


def schedule_retry(
    cfg: Settings, attempt_row: Mapping, *, now: datetime | None = None
) -> int | None:
    """Schedule the follow-up to a finished attempt; None = no retry.

    `attempt_row` is the call_attempts row that just finished. Returns the new
    attempt's id, or None when the schedule is exhausted, the attempt cap is
    reached, the window is uncomputable, or a concurrent writer won the race.
    """
    # Lazy imports per the module contract (compliance is a sibling package).
    from ..compliance import attempts as compliance_attempts
    from ..compliance import windows

    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    phone = str(attempt_row["to_phone_e164"])
    finished_n = int(attempt_row.get("attempt_number") or 1)

    delays = cfg.retry_delays()
    if finished_n - 1 >= len(delays):
        log.info("retry schedule exhausted after attempt %d for …%s", finished_n, phone[-4:])
        return None
    if compliance_attempts.attempts_for_phone(cfg, phone) >= cfg.max_attempts_total:
        log.info("attempt cap reached for …%s — no retry", phone[-4:])
        return None

    target = now + delays[finished_n - 1]
    when = windows.next_window_open(cfg, phone, target)
    if when is None:
        log.warning("cannot clamp retry into a window for …%s — not rescheduling", phone[-4:])
        return None

    contact_id = int(attempt_row["contact_id"])
    touch = str(attempt_row["touch_type"])
    row = db.query_one(cfg, _NEXT_ATTEMPT_SQL, {"contact_id": contact_id, "touch": touch})
    next_n = int(row["n"]) if row else finished_n + 1

    inserted = db.query_one(
        cfg,
        _INSERT_SQL,
        {
            "key": f"contact:{contact_id}:touch:{touch}:attempt:{next_n}",
            "contact_id": contact_id,
            "touch": touch,
            "n": next_n,
            "phone": phone,
            # Carry the variant (stable A/B identity) and the consent snapshot.
            "opener": attempt_row.get("opener_variant"),
            "consent": attempt_row.get("consent_basis"),
            "when": when,
        },
    )
    if inserted is None:
        return None
    new_id = int(inserted["id"])
    log.info(
        "retry scheduled: attempt %d for contact %d (%s) at %s (row %d)",
        next_n, contact_id, touch, when.isoformat(), new_id,
    )
    return new_id
