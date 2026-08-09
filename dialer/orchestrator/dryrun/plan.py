"""The Phase-1 deliverable: exactly what WOULD be dialed, and when. Zero calls.

Pure simulation over the synced tables — no provider traffic, no writes to
`numbers`/`number_usage`/`call_attempts`. For every audience contact it runs
the real compliance gate, projects a dial moment that honors the prospect's
local window and our calls-per-minute pacing, and assigns the caller ID the
pool would hand out (area-code match first, round-robin fallback, honoring
daily caps, ramp-by-age, and cooldown against an in-memory pool copy).

Two deliberate differences from the live gate, both about making a phase-1
CSV useful rather than uniformly "blocked":

- The PHASE gate is skipped (simulated at phase 3): a phase-1 operator runs
  this precisely to see what the live phases would do.
- KILL_SWITCH is dropped from per-row reasons: the switch halts execution,
  not planning — and with no Redis running the fail-closed switch would
  otherwise stamp every row.

Everything else — suppression, DNC, consent, windows, caps — is the same
code path the dial worker uses.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime, timedelta, timezone

from .. import db
from ..config import Settings
from ..logging_utils import get_logger
from ..models import BlockReason, TouchType

log = get_logger(__name__)

_AUDIENCE_SQL = """
SELECT c.* FROM contacts c
WHERE c.touch_type = %(touch)s
  AND lower(coalesce(c.progress_status, '')) = %(progress)s
ORDER BY c.id
LIMIT %(limit)s
"""

_PROGRESS_FOR_TOUCH = {TouchType.FIRST: "undialed", TouchType.SECOND: "dialed"}

_MAX_ATTEMPT_SQL = """
SELECT contact_id, touch_type, max(attempt_number) AS n
FROM call_attempts GROUP BY contact_id, touch_type
"""

_POOL_SQL = """
SELECT n.*, coalesce(u.dials, 0) AS dials_today
FROM numbers n
LEFT JOIN number_usage u ON u.number_id = n.id AND u.usage_date = current_date
WHERE n.status = 'active'
ORDER BY n.last_used_at NULLS FIRST, n.id
"""


@dataclass
class DryRunRow:
    contact_id: int
    justcall_contact_id: int
    name: str | None
    company_name: str | None
    phone_e164: str
    touch_type: str
    state: str | None
    local_timezone: str | None
    consent_basis: str | None
    caller_id: str | None
    caller_id_match: str | None  # "area_code" | "round_robin" | None
    would_dial_at: datetime | None
    attempt_number: int
    allowed: bool
    blocked_reasons: str  # comma-joined BlockReason values


FIELD_NAMES = [f.name for f in fields(DryRunRow)]


class _SimNumber:
    """In-memory copy of one pool number, mutated as the simulation assigns it."""

    __slots__ = ("phone_e164", "area_code", "remaining", "available_at")

    def __init__(self, phone_e164: str, area_code: str, remaining: int,
                 available_at: datetime):
        self.phone_e164 = phone_e164
        self.area_code = area_code
        self.remaining = remaining
        self.available_at = available_at


class _SimPool:
    """The selector's rules replayed against in-memory state (no DB writes)."""

    def __init__(self, cfg: Settings, rows: list[dict], now: datetime):
        # Real cap/ramp arithmetic comes from the selector so the simulation
        # can't drift from live behavior.
        from ..pool import selector

        self._cooldown = timedelta(seconds=cfg.number_cooldown_seconds)
        self._numbers: list[_SimNumber] = []
        for row in rows:
            cap = selector.effective_daily_cap(cfg, row, now=now)
            remaining = max(cap - int(row.get("dials_today") or 0), 0)
            last_used = row.get("last_used_at")
            if last_used is not None and last_used.tzinfo is None:
                last_used = last_used.replace(tzinfo=timezone.utc)
            available_at = (last_used + self._cooldown) if last_used else now
            self._numbers.append(
                _SimNumber(row["phone_e164"], str(row["area_code"]), remaining, available_at)
            )

    def assign(self, to_phone: str, at: datetime) -> tuple[str, str] | None:
        """Pick (caller_id, match_kind) for a dial at `at`, or None."""
        area = to_phone[2:5] if to_phone.startswith("+1") and len(to_phone) == 12 else None

        def usable(n: _SimNumber) -> bool:
            return n.remaining > 0 and n.available_at <= at

        candidates = [n for n in self._numbers if usable(n) and n.area_code == area]
        match = "area_code"
        if not candidates:
            candidates = [n for n in self._numbers if usable(n)]
            match = "round_robin"
        if not candidates:
            return None
        # Round-robin = least-recently-available first, mirroring the selector.
        chosen = min(candidates, key=lambda n: (n.available_at, n.phone_e164))
        chosen.remaining -= 1
        chosen.available_at = at + self._cooldown
        return chosen.phone_e164, match


class _MinuteSpreader:
    """Same shape as the planner's: earliest minute ≥ base with CPM room."""

    def __init__(self, calls_per_minute: int):
        from collections import Counter

        self._cpm = max(calls_per_minute, 1)
        self._counts: Counter[datetime] = Counter()

    def slot(self, base: datetime) -> datetime:
        minute = base.replace(second=0, microsecond=0)
        while self._counts[minute] >= self._cpm:
            minute += timedelta(minutes=1)
        self._counts[minute] += 1
        return max(base, minute)


def build_dry_run(
    cfg: Settings,
    touches: list[TouchType],
    *,
    now: datetime | None = None,
    limit: int | None = None,
) -> list[DryRunRow]:
    from .. import compliance
    from ..compliance import area_codes, windows

    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    # Phase gate deliberately simulated at 3 — see the module docstring.
    sim_cfg = cfg.model_copy(update={"phase": 3})

    max_attempts = {
        (row["contact_id"], row["touch_type"]): int(row["n"])
        for row in db.query(cfg, _MAX_ATTEMPT_SQL)
    }
    pool = _SimPool(sim_cfg, db.query(cfg, _POOL_SQL), now)
    spreader = _MinuteSpreader(cfg.calls_per_minute)

    rows: list[DryRunRow] = []
    for touch in touches:
        contacts = db.query(
            cfg,
            _AUDIENCE_SQL,
            {"touch": touch.value, "progress": _PROGRESS_FOR_TOUCH[touch],
             "limit": limit},
        )
        for contact in contacts:
            phone = str(contact["phone_e164"])
            info = area_codes.info_for_phone(phone)
            decision = compliance.check_dial_allowed(sim_cfg, contact, now=now)
            reasons = [r for r in decision.reasons if r != BlockReason.KILL_SWITCH]

            would_dial_at: datetime | None = None
            caller_id: str | None = None
            match: str | None = None
            permanent = any(r.is_permanent for r in reasons)
            if not permanent:
                base = now if not reasons else (decision.earliest_allowed or None)
                if base is not None:
                    # Slot under CPM, then make sure pacing didn't push the
                    # dial past the prospect's window close.
                    for _ in range(3):
                        slotted = spreader.slot(base)
                        if windows.is_within_window(cfg, phone, slotted):
                            would_dial_at = slotted
                            break
                        base = windows.next_window_open(cfg, phone, slotted) or slotted
                    else:
                        would_dial_at = slotted  # give the operator *a* time
                if would_dial_at is not None:
                    assigned = pool.assign(phone, would_dial_at)
                    if assigned is not None:
                        caller_id, match = assigned
                    else:
                        reasons.append(BlockReason.NO_NUMBER_AVAILABLE)

            rows.append(
                DryRunRow(
                    contact_id=int(contact["id"]),
                    justcall_contact_id=int(contact["justcall_contact_id"]),
                    name=contact.get("name"),
                    company_name=contact.get("company_name"),
                    phone_e164=phone,
                    touch_type=touch.value,
                    state=info.state if info else None,
                    local_timezone=info.tz if info else None,
                    consent_basis=contact.get("consent_basis"),
                    caller_id=caller_id,
                    caller_id_match=match,
                    would_dial_at=would_dial_at,
                    attempt_number=max_attempts.get((contact["id"], touch.value), 0) + 1,
                    allowed=not reasons,
                    blocked_reasons=",".join(r.value for r in reasons),
                )
            )
    log.info(
        "dry run: %d rows, %d dialable, %d blocked",
        len(rows), sum(r.allowed for r in rows), sum(not r.allowed for r in rows),
    )
    return rows


def summarize(rows: list[DryRunRow]) -> dict:
    """Counts for the CLI summary table."""
    from collections import Counter

    blocked: Counter[str] = Counter()
    for row in rows:
        for reason in row.blocked_reasons.split(","):
            if reason:
                blocked[reason] += 1
    return {
        "total": len(rows),
        "dialable": sum(r.allowed for r in rows),
        "with_caller_id": sum(1 for r in rows if r.caller_id),
        "blocked": dict(blocked),
    }
