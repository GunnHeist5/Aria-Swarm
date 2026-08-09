"""The dial sequence: reserve-before-dial, stale exits, block handling."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

from conftest import make_settings

import orchestrator.compliance as compliance_pkg
import orchestrator.db as db
import orchestrator.voice as voice_pkg
from orchestrator.models import BlockReason, DialDecision, NumberLease
from orchestrator.pacing import limiter
from orchestrator.pool import selector
from orchestrator.queueing import killswitch, retries, worker

NOW = datetime(2026, 8, 10, 15, 0, tzinfo=timezone.utc)

ATTEMPT = {
    "id": 7, "contact_id": 3, "touch_type": "second", "attempt_number": 1,
    "status": "queued", "to_phone_e164": "+16145550001", "opener_variant": "opener_a",
    "consent_basis": "prior_express_consent",
}
CONTACT = {
    "id": 3, "justcall_contact_id": 1003, "phone_e164": "+16145550001",
    "touch_type": "second", "contact_status": "Active", "company_name": "Drainco",
    "consent_basis": "prior_express_consent",
}


class FakeConn:
    """Answers the worker's SQL by shape; records every statement."""

    def __init__(self, attempt, contact, events):
        self._attempt, self._contact, self.events = attempt, contact, events

    def execute(self, sql, params=None):
        self.events.append((sql, params))

        class _Cur:
            def __init__(self, row):
                self._row = row

            def fetchone(self):
                return self._row

            def fetchall(self):
                return [self._row] if self._row else []

        if "FROM call_attempts" in sql:
            return _Cur(self._attempt)
        if "FROM contacts" in sql:
            return _Cur(self._contact)
        return _Cur(None)


def _wire(monkeypatch, *, attempt=None, contact=CONTACT, decision=None,
          slot=True, lease="default", provider_exc=None):
    events: list = []
    conn = FakeConn(attempt if attempt is not None else dict(ATTEMPT), contact, events)

    @contextmanager
    def fake_tx(cfg):
        yield conn

    monkeypatch.setattr(db, "tx", fake_tx)
    monkeypatch.setattr(
        db, "execute",
        lambda cfg, sql, params=None: events.append((sql, params)) or 1,
    )
    monkeypatch.setattr(killswitch, "is_engaged", lambda cfg: False)
    monkeypatch.setattr(
        compliance_pkg, "check_dial_allowed",
        lambda cfg, c, now=None: decision or DialDecision(allowed=True),
    )
    monkeypatch.setattr(limiter, "try_acquire_call_slot", lambda cfg: slot)
    if lease == "default":
        lease = NumberLease(1, "+16145559999", "614", True)
    monkeypatch.setattr(selector, "pick_number", lambda cfg, phone, now=None: lease)
    released: list = []
    monkeypatch.setattr(selector, "release_unused", lambda cfg, l: released.append(l))
    retried: list = []
    monkeypatch.setattr(retries, "schedule_retry", lambda cfg, row, now=None: retried.append(row))

    class FakeProvider:
        name = "twilio"

        def start_call(self, **kwargs):
            events.append(("start_call", kwargs))
            if provider_exc:
                raise provider_exc
            return "CA123"

    monkeypatch.setattr(voice_pkg, "get_voice_provider", lambda cfg: FakeProvider())
    return events, released, retried


def test_phase1_places_zero_calls(monkeypatch):
    events, _, _ = _wire(monkeypatch)
    assert worker._dial_attempt_sync(make_settings(phase=1), 7, now=NOW) == "phase_gate"
    assert not any(e[0] == "start_call" for e in events)


def test_stale_row_exits_without_dialing(monkeypatch):
    events, _, _ = _wire(monkeypatch, attempt={**ATTEMPT, "status": "completed"})
    assert worker._dial_attempt_sync(make_settings(phase=2), 7, now=NOW) == "stale"
    assert not any(e[0] == "start_call" for e in events)


def test_permanent_block_cancels(monkeypatch):
    decision = DialDecision(allowed=False, reasons=[BlockReason.SUPPRESSED])
    events, _, _ = _wire(monkeypatch, decision=decision)
    assert worker._dial_attempt_sync(make_settings(phase=2), 7, now=NOW) == "canceled"
    cancels = [p for sql, p in events if isinstance(sql, str) and "'canceled'" in sql]
    assert cancels and cancels[0]["error"] == "suppressed"
    assert not any(e[0] == "start_call" for e in events)


def test_paced_out_defers(monkeypatch):
    events, _, _ = _wire(monkeypatch, slot=False)
    assert worker._dial_attempt_sync(make_settings(phase=2), 7, now=NOW) == "paced"
    assert any(isinstance(sql, str) and "'scheduled'" in sql for sql, _ in events)
    assert not any(e[0] == "start_call" for e in events)


def test_reserve_commits_before_provider_call(monkeypatch):
    events, _, _ = _wire(monkeypatch)
    assert worker._dial_attempt_sync(make_settings(phase=2), 7, now=NOW) == "dialed"
    order = [
        i for i, (sql, _) in enumerate(events)
        if (isinstance(sql, str) and "status = 'dialing'" in sql) or sql == "start_call"
    ]
    kinds = [events[i][0] for i in order]
    assert "start_call" in kinds
    # The dialing reservation strictly precedes the provider call.
    assert kinds.index("start_call") > 0 and "status = 'dialing'" in kinds[0]
    # And the provider ack lands as in_progress with the call sid.
    assert any(
        isinstance(sql, str) and "'in_progress'" in sql and params.get("pcid") == "CA123"
        for sql, params in events if isinstance(params, dict)
    )


def test_provider_failure_marks_failed_and_retries(monkeypatch):
    events, released, retried = _wire(monkeypatch, provider_exc=RuntimeError("twilio down"))
    assert worker._dial_attempt_sync(make_settings(phase=2), 7, now=NOW) == "failed"
    assert released, "unused lease must be released when the dial never happened"
    assert retried, "a provider failure schedules the next attempt"
    assert any(isinstance(sql, str) and "'failed'" in sql for sql, _ in events)


def test_no_number_defers_to_tomorrow(monkeypatch):
    events, _, _ = _wire(monkeypatch, lease=None)
    assert worker._dial_attempt_sync(make_settings(phase=2), 7, now=NOW) == "no_number"
    defers = [p for sql, p in events if isinstance(sql, str) and "'scheduled'" in sql]
    assert defers and defers[0]["when"] > NOW
