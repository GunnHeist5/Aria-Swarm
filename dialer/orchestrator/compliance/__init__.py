"""The compliance gate: every hard block from SPEC §6, checked before EVERY dial.

`check_dial_allowed` is called twice per attempt on purpose — once by the
planner and again by the dial worker moments before the provider call —
because suppression, kill-switch, and window state can all change between
scheduling and dialing.

Design points:

- ALL applicable checks run and every reason is collected (no short-circuit).
  A blocked-reasons tally is an observability requirement (SPEC §9); a gate
  that stops at the first reason under-reports every other problem.
- Blocks split into permanent (cancel the attempt) and temporal (reschedule);
  `BlockReason.is_permanent` owns that classification. When the only blocks
  are temporal and a window reopening is computable, `earliest_allowed` says
  when to try again.
- The kill switch lives in orchestrator/queueing (Redis + kv_state); it is
  imported lazily so this package stands alone, and an import/lookup failure
  counts as ENGAGED — if we cannot prove dialing is enabled, it isn't.
"""

from __future__ import annotations

import importlib
from datetime import datetime, timezone
from typing import Mapping

from ..config import Settings
from ..logging_utils import get_logger
from ..models import BlockReason, DialDecision, TouchType
from . import area_codes, attempts, dnc, suppression, windows
from .phones import normalize_phone

log = get_logger(__name__)

__all__ = ["check_dial_allowed"]


def _killswitch_engaged(cfg: Settings) -> bool:
    # Lazy import: queueing is a sibling package (and pulls in Redis); the
    # compliance package must import and test without it. Fail closed on any
    # failure to consult it.
    try:
        killswitch = importlib.import_module("orchestrator.queueing.killswitch")
        return bool(killswitch.is_engaged(cfg))
    except Exception:
        log.exception("kill switch unavailable — treating as ENGAGED (fail closed)")
        return True


def _touch_of(contact: Mapping) -> TouchType:
    # Unknown/missing touch_type degrades to FIRST: in phase 2 that is the
    # blocked audience, so ambiguity fails closed.
    try:
        return TouchType(str(contact.get("touch_type") or ""))
    except ValueError:
        return TouchType.FIRST


def check_dial_allowed(
    cfg: Settings, contact: Mapping, *, now: datetime | None = None
) -> DialDecision:
    """Run every compliance check against one `contacts` row.

    Check order (per the module contract): kill switch → phase gate →
    contact_status → phone validity → suppression → consent basis → DNC →
    area code known/US → calling window → attempt cap. Phone-dependent checks
    are skipped when the phone itself is unparseable (INVALID_PHONE already
    blocks permanently, and there is nothing coherent to look up).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    reasons: list[BlockReason] = []
    earliest: datetime | None = None

    # 1. Kill switch — one command halts all dialing immediately (SPEC §8).
    if _killswitch_engaged(cfg):
        reasons.append(BlockReason.KILL_SWITCH)

    # 2. Phase gate — deliverables ship in order and code enforces it:
    #    phase 1 is dry-run only (blocks everything); phase 2 is the small
    #    second-touch slice (blocks the FIRST audience). Phase 3's Trust Hub
    #    prerequisite is gated procedurally (docs/trusthub.md), per contract.
    touch = _touch_of(contact)
    if cfg.phase == 1:
        reasons.append(BlockReason.PHASE_GATE)
    elif cfg.phase == 2 and touch == TouchType.FIRST:
        reasons.append(BlockReason.PHASE_GATE)

    # 3. Contact status as reported by JustCall.
    status = str(contact.get("contact_status") or "").strip().lower()
    if status == "dnca":
        reasons.append(BlockReason.CONTACT_DNCA)
    elif status == "invalid":
        reasons.append(BlockReason.CONTACT_INVALID)

    # 4. Phone validity — everything downstream needs a real E.164 number.
    phone = normalize_phone(str(contact.get("phone_e164") or ""))
    if phone is None:
        reasons.append(BlockReason.INVALID_PHONE)

    # 5. Internal suppression ledger — permanent, checked before every dial.
    if phone is not None and suppression.is_suppressed(cfg, phone):
        reasons.append(BlockReason.SUPPRESSED)

    # 6. Consent basis: the ROW's snapshot must be non-null (required field,
    #    no default anywhere — SPEC §6). Checked regardless of phone validity.
    if not contact.get("consent_basis"):
        reasons.append(BlockReason.NO_CONSENT_BASIS)

    if phone is not None:
        # 7. Federal DNC: a missing source blocks exactly like a listing.
        checker = dnc.get_dnc_checker(cfg)
        if not checker.available():
            reasons.append(BlockReason.DNC_SOURCE_MISSING)
        elif checker.is_listed(phone):
            reasons.append(BlockReason.FEDERAL_DNC)

        # 8. Area code must be known and (unless configured otherwise) US.
        info = area_codes.info_for_phone(phone)
        if info is None:
            reasons.append(BlockReason.UNKNOWN_AREA_CODE)
        elif info.country != "US" and not cfg.allow_non_us_nanp:
            reasons.append(BlockReason.NON_US_NUMBER)

        # 9. Calling window in the prospect's local time. When the zone is
        #    unknown this reads as outside-window with no reopen time; the
        #    area-code reasons above are the actionable signal in that case.
        if not windows.is_within_window(cfg, phone, now):
            reasons.append(BlockReason.OUTSIDE_WINDOW)
            earliest = windows.next_window_open(cfg, phone, now)

        # 10. Attempt cap across BOTH touch types, counted per phone.
        if attempts.attempts_for_phone(cfg, phone) >= cfg.max_attempts_total:
            reasons.append(BlockReason.ATTEMPT_CAP)

    decision = DialDecision(allowed=not reasons, reasons=reasons)
    # earliest_allowed only means "retry then" — attach it only when nothing
    # permanent also blocks (a canceled attempt has no retry time).
    if reasons and not decision.permanently_blocked:
        decision.earliest_allowed = earliest
    return decision
