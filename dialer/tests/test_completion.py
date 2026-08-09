"""finalize_attempt: idempotency and disposition side effects."""

from __future__ import annotations

from conftest import make_settings

import orchestrator.db as db
from orchestrator.compliance import suppression
from orchestrator.justcall import writeback
from orchestrator.models import CallResult, Disposition, Outcome
from orchestrator.pool import selector
from orchestrator.queueing import alerts, completion, retries

ROW = {
    "id": 9, "contact_id": 3, "touch_type": "second", "attempt_number": 1,
    "status": "in_progress", "to_phone_e164": "+16145550001",
    "from_number_id": 5, "disposition": None,
}
CONTACT = {"id": 3, "justcall_contact_id": 1003, "name": "O", "company_name": "Drainco"}


def _wire(monkeypatch, *, update_rows=1):
    calls = {"suppress": [], "outbox": [], "retry": [], "connect": [], "alert": []}
    monkeypatch.setattr(db, "execute", lambda cfg, sql, params=None: update_rows)
    monkeypatch.setattr(db, "query_one", lambda cfg, sql, params=None: CONTACT)
    monkeypatch.setattr(
        suppression, "suppress",
        lambda cfg, phone, reason, source: calls["suppress"].append((phone, reason)) or True,
    )
    monkeypatch.setattr(
        writeback, "enqueue_writeback",
        lambda cfg, kind, attempt_id, payload: calls["outbox"].append((kind, payload)) or 1,
    )
    monkeypatch.setattr(retries, "schedule_retry", lambda cfg, row, now=None: calls["retry"].append(row))
    monkeypatch.setattr(selector, "record_connect", lambda cfg, nid: calls["connect"].append(nid))
    monkeypatch.setattr(alerts, "send_alert", lambda cfg, text: calls["alert"].append(text))
    return calls


def test_already_terminal_row_short_circuits(monkeypatch):
    calls = _wire(monkeypatch)
    completion.finalize_attempt(
        make_settings(), {**ROW, "status": "completed"},
        CallResult(provider_call_id="CA1", outcome=Outcome.CONNECTED),
    )
    assert not any(calls.values())


def test_lost_race_skips_side_effects(monkeypatch):
    calls = _wire(monkeypatch, update_rows=0)
    completion.finalize_attempt(
        make_settings(), dict(ROW),
        CallResult(provider_call_id="CA1", outcome=Outcome.CONNECTED,
                   disposition=Disposition.OPT_OUT),
    )
    assert calls["suppress"] == [] and calls["outbox"] == []


def test_opt_out_suppresses_and_moves_dnca(monkeypatch):
    calls = _wire(monkeypatch)
    completion.finalize_attempt(
        make_settings(), dict(ROW),
        CallResult(provider_call_id="CA1", outcome=Outcome.CONNECTED,
                   disposition=Disposition.OPT_OUT, duration_sec=30),
    )
    assert calls["suppress"] == [("+16145550001", "opt_out")]
    kinds = [k for k, _ in calls["outbox"]]
    assert "justcall_dnca" in kinds and "justcall_disposition" in kinds
    assert calls["retry"] == []  # never retry an opt-out
    assert calls["connect"] == [5]


def test_relay_stored_disposition_still_fires_side_effects(monkeypatch):
    # The status callback usually has no disposition — the relay socket wrote
    # it to the row earlier. Side effects must honor the stored value.
    calls = _wire(monkeypatch)
    completion.finalize_attempt(
        make_settings(), {**ROW, "disposition": "opt_out"},
        CallResult(provider_call_id="CA1", outcome=Outcome.CONNECTED),
    )
    assert calls["suppress"] == [("+16145550001", "opt_out")]


def test_no_answer_schedules_retry(monkeypatch):
    calls = _wire(monkeypatch)
    completion.finalize_attempt(
        make_settings(), dict(ROW),
        CallResult(provider_call_id="CA1", outcome=Outcome.NO_ANSWER),
    )
    assert calls["retry"] and calls["suppress"] == []
    assert calls["connect"] == []


def test_booked_goes_to_crm_outbox_and_alerts(monkeypatch):
    calls = _wire(monkeypatch)
    completion.finalize_attempt(
        make_settings(), dict(ROW),
        CallResult(provider_call_id="CA1", outcome=Outcome.CONNECTED,
                   disposition=Disposition.BOOKED, duration_sec=200),
    )
    kinds = [k for k, _ in calls["outbox"]]
    assert "crm_booking" in kinds
    assert calls["alert"] and "BOOKED" in calls["alert"][0]
