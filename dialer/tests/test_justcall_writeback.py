"""Writeback outbox: enqueue validation, batch drain, retry-then-fail policy.

No Postgres and no network: orchestrator.db functions are monkeypatched, the
JustCall client is replaced with a recording fake, and the (not-yet-written)
queueing.alerts sibling is injected into sys.modules.
"""

from __future__ import annotations

import sys
import types

import pytest
from conftest import make_settings

from orchestrator import db
from orchestrator.config import ConfigError
import orchestrator.justcall.writeback as writeback_mod
from orchestrator.justcall.client import JustCallError
from orchestrator.justcall.writeback import enqueue_writeback, process_outbox_batch


# ------------------------------------------------------------------ helpers


class FakeDb:
    def __init__(self, pending: list[dict] | None = None) -> None:
        self.pending = pending or []
        self.updates: list[tuple[str, dict]] = []
        self.inserts: list[tuple[str, dict]] = []
        self.select_params: dict | None = None

    def query(self, cfg, sql, params=None):
        assert "writeback_outbox" in sql
        self.select_params = params
        return self.pending

    def query_one(self, cfg, sql, params=None):
        self.inserts.append((sql, params))
        return {"id": 100 + len(self.inserts)}

    def execute(self, cfg, sql, params=None):
        self.updates.append((sql, params))
        return 1


def patch_db(monkeypatch, fake: FakeDb) -> None:
    monkeypatch.setattr(db, "query", fake.query)
    monkeypatch.setattr(db, "query_one", fake.query_one)
    monkeypatch.setattr(db, "execute", fake.execute)


def install_fake_alerts(monkeypatch, records: list[str]) -> None:
    pkg = types.ModuleType("orchestrator.queueing")
    alerts = types.ModuleType("orchestrator.queueing.alerts")
    alerts.send_alert = lambda cfg, text: records.append(text)
    pkg.alerts = alerts
    monkeypatch.setitem(sys.modules, "orchestrator.queueing", pkg)
    monkeypatch.setitem(sys.modules, "orchestrator.queueing.alerts", alerts)


def dnca_row(row_id: int, *, tries: int = 0, contact_id: int = 90210392) -> dict:
    return {
        "id": row_id,
        "kind": "justcall_dnca",
        "attempt_id": None,
        "payload": {"justcall_contact_id": contact_id},
        "tries": tries,
    }


def note_row(row_id: int, *, tries: int = 0) -> dict:
    return {
        "id": row_id,
        "kind": "justcall_disposition",
        "attempt_id": 5,
        "payload": {"justcall_contact_id": 90210417, "note": "AI: interested"},
        "tries": tries,
    }


def booking_row(row_id: int) -> dict:
    return {
        "id": row_id,
        "kind": "crm_booking",
        "attempt_id": 6,
        "payload": {"when": "2026-08-10T15:00:00Z", "prospect": "Rivera Drain Pros"},
        "tries": 0,
    }


class RecordingClient:
    constructions = 0

    def __init__(self, cfg):
        type(self).constructions += 1
        self.calls: list[tuple] = []
        RecordingClient.last = self

    def set_contact_dnca(self, contact_id):
        self.calls.append(("dnca", contact_id))

    def add_contact_note(self, contact_id, note):
        self.calls.append(("note", contact_id, note))


@pytest.fixture(autouse=True)
def reset_recording_client():
    RecordingClient.constructions = 0
    yield


# ------------------------------------------------------------------ enqueue


def test_enqueue_returns_row_id(monkeypatch):
    fake = FakeDb()
    patch_db(monkeypatch, fake)
    row_id = enqueue_writeback(make_settings(), "justcall_dnca", None, {"justcall_contact_id": 1})
    assert row_id == 101
    sql, params = fake.inserts[0]
    assert "INSERT INTO writeback_outbox" in sql
    assert params["kind"] == "justcall_dnca"
    assert params["attempt_id"] is None
    assert params["payload"].obj == {"justcall_contact_id": 1}


def test_enqueue_rejects_unknown_kind(monkeypatch):
    fake = FakeDb()
    patch_db(monkeypatch, fake)
    with pytest.raises(ValueError):
        enqueue_writeback(make_settings(), "email_blast", None, {})
    assert fake.inserts == []


# ------------------------------------------------------------ batch draining


def test_process_batch_success(monkeypatch):
    fake = FakeDb([dnca_row(1), note_row(2), booking_row(3)])
    patch_db(monkeypatch, fake)
    monkeypatch.setattr(writeback_mod, "JustCallClient", RecordingClient)

    sent = process_outbox_batch(make_settings())

    assert sent == 3
    assert RecordingClient.constructions == 1  # one client for the whole batch
    assert RecordingClient.last.calls == [
        ("dnca", 90210392),
        ("note", 90210417, "AI: interested"),
    ]
    sent_updates = [u for u in fake.updates if "'sent'" in u[0]]
    assert [u[1]["id"] for u in sent_updates] == [1, 2, 3]
    assert fake.select_params == {"limit": 20}


def test_process_batch_custom_limit(monkeypatch):
    fake = FakeDb([])
    patch_db(monkeypatch, fake)
    assert process_outbox_batch(make_settings(), limit=5) == 0
    assert fake.select_params == {"limit": 5}


def test_failure_increments_tries_and_stays_pending(monkeypatch):
    fake = FakeDb([dnca_row(1, tries=0)])
    patch_db(monkeypatch, fake)

    class BoomClient(RecordingClient):
        def set_contact_dnca(self, contact_id):
            raise JustCallError("JustCall POST failed: HTTP 500")

    monkeypatch.setattr(writeback_mod, "JustCallClient", BoomClient)
    alerts: list[str] = []
    install_fake_alerts(monkeypatch, alerts)

    assert process_outbox_batch(make_settings()) == 0
    (sql, params), = fake.updates
    assert params == {
        "status": "pending",
        "tries": 1,
        "last_error": "JustCallError: JustCall POST failed: HTTP 500",
        "id": 1,
    }
    assert alerts == []


def test_eighth_failure_marks_failed_and_alerts(monkeypatch):
    fake = FakeDb([dnca_row(1, tries=7)])
    patch_db(monkeypatch, fake)

    class BoomClient(RecordingClient):
        def set_contact_dnca(self, contact_id):
            raise JustCallError("HTTP 503")

    monkeypatch.setattr(writeback_mod, "JustCallClient", BoomClient)
    alerts: list[str] = []
    install_fake_alerts(monkeypatch, alerts)

    assert process_outbox_batch(make_settings()) == 0
    (sql, params), = fake.updates
    assert params["status"] == "failed"
    assert params["tries"] == 8
    assert len(alerts) == 1 and "row 1" in alerts[0] and "justcall_dnca" in alerts[0]


def test_bad_payload_follows_retry_policy(monkeypatch):
    row = dnca_row(1)
    row["payload"] = {}  # missing justcall_contact_id
    fake = FakeDb([row])
    patch_db(monkeypatch, fake)
    monkeypatch.setattr(writeback_mod, "JustCallClient", RecordingClient)

    assert process_outbox_batch(make_settings()) == 0
    (sql, params), = fake.updates
    assert params["status"] == "pending" and params["tries"] == 1


def test_crm_booking_only_batch_needs_no_client(monkeypatch):
    fake = FakeDb([booking_row(9)])
    patch_db(monkeypatch, fake)

    def explode(cfg):
        raise AssertionError("JustCallClient must not be built for CRM-only batches")

    monkeypatch.setattr(writeback_mod, "JustCallClient", explode)
    assert process_outbox_batch(make_settings()) == 1


def test_missing_credentials_fail_closed_before_burning_tries(monkeypatch):
    fake = FakeDb([dnca_row(1)])
    patch_db(monkeypatch, fake)
    # Real client + no credentials: the batch refuses to run (ConfigError)
    # instead of incrementing tries on every row.
    with pytest.raises(ConfigError):
        process_outbox_batch(make_settings())
    assert fake.updates == []
