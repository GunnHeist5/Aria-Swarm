"""Webhook: signature verification (fail closed), dedupe, opt-out suppression,
qualifying-disposition follow-up scheduling.

No Postgres: orchestrator.db functions are monkeypatched. The compliance
sibling package (agent B) is injected as fakes in sys.modules — webhook.py
imports it lazily inside function bodies.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest
from conftest import make_settings

from orchestrator import db
import orchestrator.justcall.webhook as webhook_mod
from orchestrator.justcall.webhook import handle_call_completed, verify_signature

SECRET = "whsec_abcdef123456"
BODY = b'{"call_id": "c-777", "disposition": "Interested"}'

CONTACT_ROW = {
    "id": 42,
    "justcall_contact_id": 90210417,
    "phone_e164": "+16145550142",
    "consent_basis": "prior rep relationship",
}

FIXED_WINDOW_OPEN = datetime(2027, 1, 5, 15, 0, tzinfo=timezone.utc)


# ------------------------------------------------------------------ helpers


def sig_hex(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def sig_b64(body: bytes, secret: str = SECRET) -> str:
    return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


def fake_normalize(raw: str) -> str | None:
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return f"+1{digits}" if len(digits) == 10 else None


def install_fake_compliance(monkeypatch, *, suppress=None, next_open=None) -> None:
    pkg = types.ModuleType("orchestrator.compliance")
    phones = types.ModuleType("orchestrator.compliance.phones")
    phones.normalize_phone = fake_normalize
    suppression = types.ModuleType("orchestrator.compliance.suppression")
    suppression.suppress = suppress or (lambda cfg, phone, reason, source: True)
    windows = types.ModuleType("orchestrator.compliance.windows")
    windows.next_window_open = next_open or (lambda cfg, phone, when: FIXED_WINDOW_OPEN)
    pkg.phones, pkg.suppression, pkg.windows = phones, suppression, windows
    for name, module in {
        "orchestrator.compliance": pkg,
        "orchestrator.compliance.phones": phones,
        "orchestrator.compliance.suppression": suppression,
        "orchestrator.compliance.windows": windows,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)


class FakeDb:
    def __init__(self) -> None:
        self.event_insert_result = 1
        self.max_attempt = 0
        self.contacts_by_jc_id: dict[int, dict] = {}
        self.contacts_by_phone: dict[str, dict] = {}
        self.executes: list[tuple[str, dict]] = []
        self.attempt_params: dict | None = None

    def execute(self, cfg, sql, params=None):
        self.executes.append((sql, params))
        if "webhook_events" in sql:
            return self.event_insert_result
        if "call_attempts" in sql:
            self.attempt_params = params
            return 1
        return 1

    def query_one(self, cfg, sql, params=None):
        if "MAX(attempt_number)" in sql:
            return {"n": self.max_attempt}
        if "justcall_contact_id = " in sql:
            return self.contacts_by_jc_id.get(params["justcall_contact_id"])
        if "phone_e164 = " in sql:
            return self.contacts_by_phone.get(params["phone_e164"])
        return None


@pytest.fixture
def fdb(monkeypatch) -> FakeDb:
    fake = FakeDb()
    monkeypatch.setattr(db, "execute", fake.execute)
    monkeypatch.setattr(db, "query_one", fake.query_one)
    return fake


@pytest.fixture
def enqueued(monkeypatch) -> list[tuple]:
    calls: list[tuple] = []
    monkeypatch.setattr(
        webhook_mod.writeback,
        "enqueue_writeback",
        lambda cfg, kind, attempt_id, payload: calls.append((kind, attempt_id, payload)) or 1,
    )
    return calls


# --------------------------------------------------------- verify_signature


def test_verify_signature_hex():
    assert verify_signature(BODY, {"x-justcall-signature": sig_hex(BODY)}, SECRET)


def test_verify_signature_base64():
    assert verify_signature(BODY, {"x-justcall-signature": sig_b64(BODY)}, SECRET)


def test_verify_signature_sha256_prefix():
    assert verify_signature(BODY, {"x-justcall-signature": "sha256=" + sig_hex(BODY)}, SECRET)


def test_verify_signature_alternate_and_case_insensitive_headers():
    assert verify_signature(BODY, {"X-Webhook-Signature": sig_hex(BODY)}, SECRET)
    assert verify_signature(BODY, {"X-SIGNATURE": sig_hex(BODY)}, SECRET)


def test_verify_signature_rejects_wrong_digest():
    assert not verify_signature(BODY, {"x-justcall-signature": sig_hex(b"other body")}, SECRET)
    assert not verify_signature(BODY, {"x-justcall-signature": sig_hex(BODY, "wrong secret")}, SECRET)


def test_verify_signature_fails_closed():
    assert not verify_signature(BODY, {}, SECRET)                                # no header
    assert not verify_signature(BODY, {"x-justcall-signature": sig_hex(BODY)}, "")  # no secret


# ---------------------------------------------------- handle_call_completed


def test_duplicate_event_short_circuits(fdb, monkeypatch):
    install_fake_compliance(monkeypatch)
    fdb.event_insert_result = 0
    result = handle_call_completed(make_settings(), {"call_id": "c-1", "disposition": "Interested"})
    assert result == "duplicate"
    assert fdb.attempt_params is None


def test_missing_call_id_ignored(fdb, monkeypatch):
    install_fake_compliance(monkeypatch)
    assert handle_call_completed(make_settings(), {"disposition": "Interested"}) == "ignored"
    assert fdb.executes == []  # nothing recorded without a dedupe key


def test_dedupe_row_recorded(fdb, monkeypatch):
    install_fake_compliance(monkeypatch)
    handle_call_completed(make_settings(), {"call_id": "c-2", "disposition": "meh"})
    sql, params = fdb.executes[0]
    assert "webhook_events" in sql and "DO NOTHING" in sql
    assert params["external_id"] == "c-2"
    assert params["event_type"] == "call_completed"


def test_opt_out_suppresses_and_enqueues_dnca(fdb, enqueued, monkeypatch):
    suppress_calls: list[tuple] = []
    install_fake_compliance(
        monkeypatch,
        suppress=lambda cfg, phone, reason, source: suppress_calls.append((phone, reason, source)) or True,
    )
    fdb.contacts_by_jc_id[90210417] = CONTACT_ROW
    payload = {
        "call_id": "c-3",
        "contact_id": 90210417,
        "disposition": "Not Interested - Do Not Call",
    }
    assert handle_call_completed(make_settings(), payload) == "suppressed"
    assert suppress_calls == [("+16145550142", "opt_out", "webhook:c-3")]
    assert enqueued == [("justcall_dnca", None, {"justcall_contact_id": 90210417})]


def test_opt_out_by_phone_without_synced_contact(fdb, enqueued, monkeypatch):
    suppress_calls: list[tuple] = []
    install_fake_compliance(
        monkeypatch,
        suppress=lambda cfg, phone, reason, source: suppress_calls.append((phone, reason, source)) or True,
    )
    payload = {"call_id": "c-4", "contact_number": "614-555-0142", "disposition": "DNC"}
    assert handle_call_completed(make_settings(), payload) == "suppressed"
    assert suppress_calls == [("+16145550142", "opt_out", "webhook:c-4")]
    assert enqueued == []  # no JustCall contact id known → nothing to write back


def test_opt_out_with_nothing_actionable(fdb, enqueued, monkeypatch):
    install_fake_compliance(monkeypatch)
    payload = {"call_id": "c-5", "disposition": "do not call"}
    assert handle_call_completed(make_settings(), payload) == "ignored"


def test_qualifying_disposition_schedules_followup(fdb, monkeypatch):
    window_args: list[tuple] = []

    def next_open(cfg, phone, when):
        window_args.append((phone, when))
        return FIXED_WINDOW_OPEN

    install_fake_compliance(monkeypatch, next_open=next_open)
    fdb.contacts_by_jc_id[90210417] = CONTACT_ROW
    fdb.max_attempt = 1
    cfg = make_settings()  # default qualifying includes "Interested"

    result = handle_call_completed(
        cfg, {"call_id": "c-6", "contact_id": 90210417, "disposition": "interested"}
    )

    assert result == "scheduled"
    params = fdb.attempt_params
    assert params["idempotency_key"] == "contact:42:touch:second:attempt:2"
    assert params["attempt_number"] == 2
    assert params["to_phone_e164"] == "+16145550142"
    assert params["consent_basis"] == "prior rep relationship"
    assert params["scheduled_for"] == FIXED_WINDOW_OPEN  # clamped into legal window

    # The clamp was asked about (roughly) now + followup_delay_hours.
    (phone, when), = window_args
    assert phone == "+16145550142"
    delta = when - datetime.now(timezone.utc)
    assert timedelta(hours=cfg.followup_delay_hours - 1) < delta <= timedelta(
        hours=cfg.followup_delay_hours
    )


def test_qualifying_without_synced_contact_ignored(fdb, monkeypatch):
    install_fake_compliance(monkeypatch)
    payload = {"call_id": "c-7", "contact_id": 555, "disposition": "Interested"}
    assert handle_call_completed(make_settings(), payload) == "ignored"
    assert fdb.attempt_params is None


def test_non_qualifying_disposition_ignored(fdb, monkeypatch):
    install_fake_compliance(monkeypatch)
    fdb.contacts_by_jc_id[90210417] = CONTACT_ROW
    payload = {"call_id": "c-8", "contact_id": 90210417, "disposition": "Ghosted"}
    assert handle_call_completed(make_settings(), payload) == "ignored"
    assert fdb.attempt_params is None


def test_qualifying_config_matching_is_case_insensitive(fdb, monkeypatch):
    install_fake_compliance(monkeypatch)
    fdb.contacts_by_jc_id[90210417] = CONTACT_ROW
    cfg = make_settings(justcall_qualifying_dispositions=["Callback"])
    payload = {"call_id": "c-9", "contact_id": 90210417, "disposition": "CALLBACK"}
    assert handle_call_completed(cfg, payload) == "scheduled"


def test_schedule_followup_without_window_keeps_delay(fdb, monkeypatch):
    install_fake_compliance(monkeypatch, next_open=lambda cfg, phone, when: None)
    now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
    cfg = make_settings(followup_delay_hours=20)

    assert webhook_mod._schedule_followup(cfg, CONTACT_ROW, now=now) == "scheduled"
    assert fdb.attempt_params["scheduled_for"] == now + timedelta(hours=20)
    assert fdb.attempt_params["attempt_number"] == 1
