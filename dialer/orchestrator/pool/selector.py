"""Caller-ID selection: local presence with hard per-number budgets.

Why this shape: burning a number's reputation is cheaper than we think and
nearly impossible to undo, so the budget checks (daily cap, ramp-by-age,
cooldown) are enforced *atomically* — one transaction locks the candidate
rows, re-checks committed usage, and increments `number_usage.dials` +
`last_used_at` before the lease is returned. Two workers can therefore never
share the 150th dial of a number's day. The usage increment IS the lease;
`release_unused()` hands it back when the dial never actually happened.

Day boundaries are UTC on purpose: the cap is a reputation budget, not a
compliance window, so one uniform clock beats per-number local-midnight math.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Mapping

from .. import db
from ..config import Settings
from ..logging_utils import get_logger
from ..models import NumberLease

log = get_logger(__name__)

# Lock every active row while choosing. The pool is small (tens of numbers),
# so a full lock beats per-row conflict/retry loops — and it makes the
# cap/cooldown re-check race-free by construction. `FOR UPDATE OF n` is legal
# here because `numbers` is the non-nullable side of the join.
_CANDIDATES_SQL = """
SELECT n.id, n.phone_e164, n.area_code, n.daily_cap, n.purchased_at, n.last_used_at,
       COALESCE(u.dials, 0) AS dials_today
FROM numbers n
LEFT JOIN number_usage u
       ON u.number_id = n.id AND u.usage_date = %(today)s
WHERE n.status = 'active'
FOR UPDATE OF n
"""

_BUMP_USAGE_SQL = """
INSERT INTO number_usage (number_id, usage_date, dials)
VALUES (%(number_id)s, %(today)s, 1)
ON CONFLICT (number_id, usage_date) DO UPDATE SET dials = number_usage.dials + 1
"""

_TOUCH_NUMBER_SQL = """
UPDATE numbers SET last_used_at = %(now)s WHERE id = %(number_id)s
"""

# GREATEST() guards the (rare) release that crosses UTC midnight: we would
# decrement a day the dial was never counted against, so clamp at zero rather
# than go negative. The error direction is safe — we under-spend the cap.
_RELEASE_SQL = """
UPDATE number_usage SET dials = GREATEST(dials - 1, 0)
WHERE number_id = %(number_id)s AND usage_date = %(today)s
"""

_CONNECT_SQL = """
INSERT INTO number_usage (number_id, usage_date, connects)
VALUES (%(number_id)s, %(today)s, 1)
ON CONFLICT (number_id, usage_date) DO UPDATE SET connects = number_usage.connects + 1
"""


def _aware(dt: datetime | None) -> datetime | None:
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


def _utc_today(now: datetime) -> date:
    return now.astimezone(timezone.utc).date()


def _age_days(purchased_at: object, now: datetime) -> int:
    """Whole days since purchase; a missing timestamp counts as brand new.

    Treating "unknown age" as day zero is the conservative direction: an
    unramped allowance on an old number costs a few dials, the reverse costs
    reputation.
    """
    if not isinstance(purchased_at, datetime):
        return 0
    purchased = _aware(purchased_at)
    assert purchased is not None
    return max((now - purchased).days, 0)


def ramp_allowance(cfg: Settings, age_days: int) -> int | None:
    """Dial allowance for a number `age_days` old; None once past the ramp.

    `ramp_schedule[k]` is the allowance on day k (day 0 = purchase day).
    Beyond the schedule the number is fully warmed and only the daily cap
    applies (None = "no ramp limit"), so a per-row `daily_cap` larger than the
    last ramp step is honored once ramping ends.
    """
    schedule = cfg.ramp_schedule
    if not schedule or age_days >= len(schedule):
        return None
    return int(schedule[max(age_days, 0)])


def effective_daily_cap(cfg: Settings, row: Mapping[str, Any], *, now: datetime) -> int:
    """min(per-number cap, ramp allowance): today's enforced dial budget."""
    base = int(row.get("daily_cap") or cfg.per_number_daily_cap)
    allowance = ramp_allowance(cfg, _age_days(row.get("purchased_at"), now))
    return base if allowance is None else min(base, allowance)


def _eligible(cfg: Settings, row: Mapping[str, Any], now: datetime) -> bool:
    if int(row.get("dials_today") or 0) >= effective_daily_cap(cfg, row, now=now):
        return False
    last_used = _aware(row.get("last_used_at"))  # type: ignore[arg-type]
    if last_used is not None and (now - last_used).total_seconds() < cfg.number_cooldown_seconds:
        return False
    return True


def _area_code_of(phone: str) -> str | None:
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        return digits[1:4]
    if len(digits) == 10:
        return digits[:3]
    return None


def _choose(
    cfg: Settings, rows: list[dict[str, Any]], want_area: str | None, now: datetime
) -> tuple[dict[str, Any] | None, bool]:
    """Pick the least-recently-used eligible number, area-code matches first.

    Selection is done in Python (not ORDER BY) so the round-robin + budget
    logic is one testable path regardless of how the rows arrive.
    """
    def lru_key(row: Mapping[str, Any]) -> tuple:
        last_used = _aware(row.get("last_used_at"))  # type: ignore[arg-type]
        # Never-used numbers sort first (round-robin by last_used_at asc).
        return (last_used is not None, last_used or now, row["id"])

    matched = sorted(
        (r for r in rows if want_area and r.get("area_code") == want_area), key=lru_key
    )
    others = sorted(
        (r for r in rows if not (want_area and r.get("area_code") == want_area)), key=lru_key
    )
    for row in matched:
        if _eligible(cfg, row, now):
            return row, True
    for row in others:
        if _eligible(cfg, row, now):
            return row, False
    return None, False


def pick_number(
    cfg: Settings, to_phone_e164: str, *, now: datetime | None = None
) -> NumberLease | None:
    """Lease a caller ID for one dial, or None when the whole pool is spent.

    The returned lease has already incremented today's usage inside the same
    transaction that inspected it — that increment is the reservation.
    """
    now = _aware(now) or datetime.now(timezone.utc)
    today = _utc_today(now)
    want_area = _area_code_of(to_phone_e164)
    with db.tx(cfg) as conn:
        rows = conn.execute(_CANDIDATES_SQL, {"today": today}).fetchall()
        pick, exact = _choose(cfg, rows, want_area, now)
        if pick is None:
            log.warning(
                "number pool exhausted (%d active numbers, target area %s)",
                len(rows), want_area,
            )
            return None
        conn.execute(_BUMP_USAGE_SQL, {"number_id": pick["id"], "today": today})
        conn.execute(_TOUCH_NUMBER_SQL, {"number_id": pick["id"], "now": now})
    return NumberLease(
        number_id=int(pick["id"]),
        phone_e164=str(pick["phone_e164"]),
        area_code=str(pick["area_code"]),
        exact_area_match=exact,
    )


def release_unused(cfg: Settings, lease: NumberLease) -> None:
    """Hand back a lease whose dial never happened (gate/provider refusal).

    Only the usage counter is returned; `last_used_at` stays bumped, so an
    aborted dial still costs the cooldown. That bias is deliberate — cheaper
    to under-use a number than to hammer it after repeated failures.
    """
    today = _utc_today(datetime.now(timezone.utc))
    db.execute(cfg, _RELEASE_SQL, {"number_id": lease.number_id, "today": today})


def record_connect(cfg: Settings, number_id: int) -> None:
    """Count one answered call for the number's connect-rate health stats."""
    today = _utc_today(datetime.now(timezone.utc))
    db.execute(cfg, _CONNECT_SQL, {"number_id": number_id, "today": today})
