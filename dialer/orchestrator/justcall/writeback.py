"""Writeback outbox: AI-call results flow back to JustCall (and later a CRM).

Outbox pattern per SPEC §7/§8: side effects on external systems are recorded
as durable rows in the same Postgres that owns call state, then drained by a
worker cron. A crash between "call finished" and "JustCall updated" therefore
loses nothing — the row stays pending and is retried until it lands or is
declared failed (8 tries) with an alert.
"""

from __future__ import annotations

from typing import Any, Mapping

from orchestrator import db
from orchestrator.config import Settings
from orchestrator.logging_utils import get_logger

from .client import JustCallClient

log = get_logger(__name__)

VALID_KINDS = frozenset({"justcall_dnca", "justcall_disposition", "crm_booking"})
MAX_TRIES = 8

_JUSTCALL_KINDS = frozenset({"justcall_dnca", "justcall_disposition"})

_ENQUEUE_SQL = """
INSERT INTO writeback_outbox (kind, attempt_id, payload)
VALUES (%(kind)s, %(attempt_id)s, %(payload)s)
RETURNING id
"""

_SELECT_PENDING_SQL = """
SELECT id, kind, attempt_id, payload, tries
FROM writeback_outbox
WHERE status = 'pending'
ORDER BY created_at, id
LIMIT %(limit)s
"""

_MARK_SENT_SQL = """
UPDATE writeback_outbox
SET status = 'sent', tries = tries + 1, sent_at = now(), last_error = NULL
WHERE id = %(id)s
"""

_MARK_FAILURE_SQL = """
UPDATE writeback_outbox
SET status = %(status)s, tries = %(tries)s, last_error = %(last_error)s
WHERE id = %(id)s
"""


class _LoggingCrmSink:
    """CrmSink stub — the booking target is TBD (SPEC §7). Logs field names
    only (never values: booking details carry contact data) so bookings stay
    visible in ops output until a real CRM lands."""

    def push_booking(self, *, attempt_id: int | None, details: Mapping[str, object]) -> None:
        log.info(
            "CRM booking (stub sink) attempt_id=%s detail_fields=%s",
            attempt_id, sorted(details),
        )


_crm_sink = _LoggingCrmSink()


def enqueue_writeback(cfg: Settings, kind: str, attempt_id: int | None, payload: dict) -> int:
    """Record one writeback intent; returns the outbox row id."""
    if kind not in VALID_KINDS:
        raise ValueError(f"unknown writeback kind {kind!r} (valid: {sorted(VALID_KINDS)})")
    row = db.query_one(
        cfg,
        _ENQUEUE_SQL,
        {"kind": kind, "attempt_id": attempt_id, "payload": db.Jsonb(payload)},
    )
    if row is None:
        raise RuntimeError("outbox insert returned no row")
    return int(row["id"])


def process_outbox_batch(cfg: Settings, limit: int = 20) -> int:
    """Drain up to `limit` pending outbox rows; returns rows successfully sent.

    A per-row failure increments tries and leaves the row pending (failed +
    alert at MAX_TRIES). Missing JustCall credentials are a config problem,
    not a delivery failure — the client is built eagerly so ConfigError
    propagates instead of burning tries on every row.
    """
    rows = db.query(cfg, _SELECT_PENDING_SQL, {"limit": limit})
    client = (
        JustCallClient(cfg)
        if any(row["kind"] in _JUSTCALL_KINDS for row in rows)
        else None
    )
    sent = 0
    for row in rows:
        try:
            _dispatch(row, client)
        except Exception as exc:  # noqa: BLE001 — any send error follows the retry policy
            _mark_failure(cfg, row, exc)
        else:
            db.execute(cfg, _MARK_SENT_SQL, {"id": row["id"]})
            sent += 1
    return sent


# ----------------------------------------------------------------- internals


def _dispatch(row: Mapping[str, Any], client: JustCallClient | None) -> None:
    kind = row["kind"]
    payload: dict = row["payload"] or {}
    if kind == "justcall_dnca":
        assert client is not None  # built eagerly above for justcall kinds
        client.set_contact_dnca(int(payload["justcall_contact_id"]))
    elif kind == "justcall_disposition":
        assert client is not None
        client.add_contact_note(
            int(payload["justcall_contact_id"]), str(payload.get("note") or "")
        )
    elif kind == "crm_booking":
        _crm_sink.push_booking(attempt_id=row.get("attempt_id"), details=payload)
    else:
        # enqueue_writeback validates kinds, so this only fires on rows written
        # by other code paths; it follows the same retry-then-fail policy.
        raise RuntimeError(f"unknown outbox kind {kind!r}")


def _mark_failure(cfg: Settings, row: Mapping[str, Any], exc: Exception) -> None:
    tries = int(row["tries"]) + 1
    status = "failed" if tries >= MAX_TRIES else "pending"
    error_text = f"{type(exc).__name__}: {exc}"[:500]
    db.execute(
        cfg,
        _MARK_FAILURE_SQL,
        {"status": status, "tries": tries, "last_error": error_text, "id": row["id"]},
    )
    log.warning(
        "writeback outbox row %s (%s) failed (try %d/%d): %s",
        row["id"], row["kind"], tries, MAX_TRIES, error_text,
    )
    if status == "failed":
        _alert(
            cfg,
            f"writeback outbox row {row['id']} ({row['kind']}) permanently failed "
            f"after {tries} tries: {error_text}",
        )


def _alert(cfg: Settings, text: str) -> None:
    try:
        from orchestrator.queueing.alerts import send_alert  # lazy: sibling package
    except ImportError:
        # Sibling module not present yet — never let alerting break the drain.
        log.error("ALERT (queueing.alerts unavailable): %s", text)
        return
    send_alert(cfg, text)
