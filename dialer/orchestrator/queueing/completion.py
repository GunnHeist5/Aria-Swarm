"""Shared completion path: one function makes an attempt terminal.

Called from the Twilio status-callback handler AND the reconciler, so it must
be idempotent — the UPDATE carries a `status NOT IN (terminal)` guard, and a
zero rowcount means another finalizer won: we do nothing, especially not the
side effects (a duplicate webhook must never suppress twice, book twice, or
schedule a second retry).

Field merging uses COALESCE(new, existing): the ConversationRelay socket may
have already persisted transcript/disposition before the status callback
arrives, and a terminal update without those fields must not erase them.

Side effects run *after* the terminal status commits, each in its own
transaction. A crash in between can lose an outbox row or a retry — never a
suppression-before-status inconsistency in the dangerous direction — and the
reconciler/report surfaces the gap.
"""

from __future__ import annotations

from typing import Mapping

from .. import db
from ..config import Settings
from ..logging_utils import get_logger
from ..models import CallCosts, CallResult, Disposition, Outcome
from ..pool import selector
from . import alerts, retries

log = get_logger(__name__)

_TERMINAL = ("completed", "failed", "canceled", "orphaned")

_RETRY_OUTCOMES = (Outcome.NO_ANSWER, Outcome.BUSY, Outcome.FAILED)
_SUPPRESS_DISPOSITIONS = (Disposition.OPT_OUT, Disposition.DISQUALIFIED)

# The status guard duplicates _TERMINAL in SQL text (kept literal so the
# statement is greppable); the Python-side check above it is only a fast path.
_FINALIZE_SQL = """
UPDATE call_attempts SET
    status = %(status)s,
    outcome = %(outcome)s,
    disposition = COALESCE(%(disposition)s, disposition),
    duration_sec = COALESCE(%(duration_sec)s, duration_sec),
    transcript = COALESCE(%(transcript)s, transcript),
    recording_url = COALESCE(%(recording_url)s, recording_url),
    provider_call_id = COALESCE(provider_call_id, %(provider_call_id)s),
    cost_voice_usd = %(cost_voice)s,
    cost_relay_usd = %(cost_relay)s,
    cost_intelligence_usd = %(cost_intelligence)s,
    cost_llm_usd = %(cost_llm)s,
    cost_total_usd = %(cost_total)s,
    ended_at = COALESCE(ended_at, now()),
    updated_at = now()
WHERE id = %(id)s
  AND status NOT IN ('completed', 'failed', 'canceled', 'orphaned')
"""

_CONTACT_SQL = """
SELECT id, justcall_contact_id, name, company_name
FROM contacts WHERE id = %(id)s
"""


def _status_for(outcome: Outcome) -> str:
    if outcome == Outcome.FAILED:
        return "failed"
    if outcome == Outcome.CANCELED:
        return "canceled"
    return "completed"


def _estimate(cfg: Settings, result: CallResult) -> CallCosts:
    # Lazy import: report/ is a sibling package. Cost estimation failing must
    # never block the terminal commit or the compliance side effects — fall
    # back to whatever the provider/relay already measured.
    try:
        from ..report import costs as report_costs
        return report_costs.estimate_costs(
            cfg,
            duration_sec=result.duration_sec,
            provider_voice_usd=result.costs.voice_usd or None,
            llm_usd=result.costs.llm_usd or None,
        )
    except Exception:
        log.exception("cost estimation unavailable — persisting provider-reported costs")
        return result.costs


def _effective_disposition(attempt_row: Mapping, result: CallResult) -> Disposition | None:
    """The result's disposition, else the one the relay already stored.

    The status callback often arrives without a disposition (the relay socket
    wrote it to the row minutes earlier); side effects must still fire off the
    stored value or an opt-out could slip through un-suppressed.
    """
    if result.disposition is not None:
        return result.disposition
    raw = attempt_row.get("disposition")
    if not raw:
        return None
    try:
        return Disposition(str(raw))
    except ValueError:
        log.warning("unknown stored disposition %r on attempt %s", raw, attempt_row.get("id"))
        return None


def finalize_attempt(cfg: Settings, attempt_row: Mapping, result: CallResult) -> None:
    """Persist a CallResult and run disposition side effects. Idempotent."""
    attempt_id = int(attempt_row["id"])
    if str(attempt_row.get("status")) in _TERMINAL:
        log.info("attempt %d already terminal — finalize skipped", attempt_id)
        return

    costs = _estimate(cfg, result)
    updated = db.execute(
        cfg,
        _FINALIZE_SQL,
        {
            "id": attempt_id,
            "status": _status_for(result.outcome),
            "outcome": result.outcome.value,
            "disposition": result.disposition.value if result.disposition else None,
            "duration_sec": result.duration_sec,
            "transcript": result.transcript,
            "recording_url": result.recording_url,
            "provider_call_id": result.provider_call_id,
            "cost_voice": costs.voice_usd,
            "cost_relay": costs.relay_usd,
            "cost_intelligence": costs.intelligence_usd,
            "cost_llm": costs.llm_usd,
            "cost_total": costs.total_usd,
        },
    )
    if updated == 0:
        # Concurrent finalizer (duplicate webhook / reconciler race) won.
        log.info("attempt %d finalized elsewhere — side effects skipped", attempt_id)
        return

    phone = str(attempt_row["to_phone_e164"])
    disposition = _effective_disposition(attempt_row, result)

    if result.outcome == Outcome.CONNECTED and attempt_row.get("from_number_id"):
        selector.record_connect(cfg, int(attempt_row["from_number_id"]))

    contact = db.query_one(cfg, _CONTACT_SQL, {"id": attempt_row["contact_id"]})
    justcall_id = contact.get("justcall_contact_id") if contact else None

    # Lazy imports per the module contract: compliance/justcall are siblings.
    from ..compliance import suppression
    from ..justcall import writeback

    if disposition in _SUPPRESS_DISPOSITIONS:
        # Permanent and immediate (SPEC §6) — the ledger before anything else.
        suppression.suppress(cfg, phone, disposition.value, f"attempt:{attempt_id}")
        if justcall_id is not None:
            writeback.enqueue_writeback(
                cfg, "justcall_dnca", attempt_id, {"justcall_contact_id": justcall_id}
            )

    if disposition == Disposition.BOOKED:
        writeback.enqueue_writeback(
            cfg,
            "crm_booking",
            attempt_id,
            {
                "attempt_id": attempt_id,
                "contact_id": int(attempt_row["contact_id"]),
                "justcall_contact_id": justcall_id,
                "company_name": contact.get("company_name") if contact else None,
            },
        )
        alerts.send_alert(cfg, f"Meeting BOOKED on attempt {attempt_id} (…{phone[-4:]})")

    if result.outcome in _RETRY_OUTCOMES and disposition not in _SUPPRESS_DISPOSITIONS:
        retries.schedule_retry(cfg, attempt_row)

    if justcall_id is not None:
        note = (
            f"AI dialer attempt {attempt_row.get('attempt_number')}: "
            f"outcome={result.outcome.value}"
            + (f", disposition={disposition.value}" if disposition else "")
            + (f", duration={result.duration_sec}s" if result.duration_sec else "")
        )
        writeback.enqueue_writeback(
            cfg,
            "justcall_disposition",
            attempt_id,
            {"justcall_contact_id": justcall_id, "note": note},
        )
    else:
        log.warning(
            "attempt %d has no resolvable justcall_contact_id — no JustCall writeback",
            attempt_id,
        )
