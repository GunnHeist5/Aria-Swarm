"""Webhook routes: fail-closed verification, dedupe, finalization wiring."""

from __future__ import annotations

import hashlib
import hmac
import json

from conftest import make_settings
from fastapi.testclient import TestClient

import orchestrator.db as db
import orchestrator.server as server
from orchestrator.justcall import webhook as justcall_webhook
from orchestrator.voice.twilio import provider as twilio_provider

SECRET = "whsec_test_secret_value"


def _client(monkeypatch, cfg=None):
    monkeypatch.setattr(db, "migrate", lambda cfg: [])
    cfg = cfg or make_settings(justcall_webhook_secret=SECRET)
    return TestClient(server.create_app(cfg)), cfg


def _signed(body: bytes) -> dict:
    digest = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return {"x-justcall-signature": digest, "content-type": "application/json"}


def test_justcall_webhook_rejects_unsigned(monkeypatch):
    client, _ = _client(monkeypatch)
    resp = client.post("/webhooks/justcall/call-completed", content=b"{}")
    assert resp.status_code == 401


def test_justcall_webhook_rejects_bad_signature(monkeypatch):
    client, _ = _client(monkeypatch)
    resp = client.post(
        "/webhooks/justcall/call-completed",
        content=b"{}",
        headers={"x-justcall-signature": "deadbeef"},
    )
    assert resp.status_code == 401


def test_justcall_webhook_accepts_signed_and_dispatches(monkeypatch):
    client, _ = _client(monkeypatch)
    seen = {}

    def fake_handle(cfg, payload):
        seen.update(payload)
        return "scheduled"

    monkeypatch.setattr(justcall_webhook, "handle_call_completed", fake_handle)
    body = json.dumps({"call_id": 42, "disposition": "Interested"}).encode()
    resp = client.post("/webhooks/justcall/call-completed", content=body, headers=_signed(body))
    assert resp.status_code == 200
    assert resp.json() == {"result": "scheduled"}
    assert seen["call_id"] == 42


def test_justcall_webhook_400_on_garbage_json(monkeypatch):
    client, _ = _client(monkeypatch)
    body = b"not json"
    resp = client.post("/webhooks/justcall/call-completed", content=body, headers=_signed(body))
    assert resp.status_code == 400


def test_twilio_status_rejects_invalid_signature(monkeypatch):
    client, _ = _client(monkeypatch)
    monkeypatch.setattr(
        twilio_provider, "validate_twilio_signature", lambda cfg, url, params, sig: False
    )
    resp = client.post("/webhooks/twilio/status", data={"CallSid": "CA1", "CallStatus": "completed"})
    assert resp.status_code == 403


def test_twilio_status_finalizes_when_signed(monkeypatch):
    client, _ = _client(monkeypatch)
    monkeypatch.setattr(
        twilio_provider, "validate_twilio_signature", lambda cfg, url, params, sig: True
    )
    calls = []
    monkeypatch.setattr(server, "_finalize_from_status", lambda cfg, form: calls.append(form) or "finalized")
    resp = client.post(
        "/webhooks/twilio/status",
        data={"CallSid": "CA1", "CallStatus": "completed", "CallDuration": "63"},
        headers={"x-twilio-signature": "sig"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"result": "finalized"}
    assert calls[0]["CallSid"] == "CA1"


def test_finalize_from_status_ignores_nonterminal_and_dedupes(monkeypatch):
    cfg = make_settings()
    # Non-terminal status short-circuits before any DB access.
    assert server._finalize_from_status(cfg, {"CallSid": "CA1", "CallStatus": "ringing"}) == "ignored"

    # Terminal but already-seen event: dedupe insert returns 0 rows.
    monkeypatch.setattr(db, "execute", lambda cfg, sql, params=None: 0)
    assert (
        server._finalize_from_status(cfg, {"CallSid": "CA1", "CallStatus": "completed"})
        == "duplicate"
    )


def test_finalize_from_status_unknown_call(monkeypatch):
    cfg = make_settings()
    monkeypatch.setattr(db, "execute", lambda cfg, sql, params=None: 1)
    monkeypatch.setattr(db, "query_one", lambda cfg, sql, params=None: None)
    assert (
        server._finalize_from_status(cfg, {"CallSid": "CAx", "CallStatus": "no-answer"})
        == "unknown"
    )


def test_healthz_reports_down_dependencies(monkeypatch):
    client, _ = _client(monkeypatch)
    monkeypatch.setattr(db, "query_one", lambda cfg, sql, params=None: {"ok": 1})
    # redis_url in test settings points at port 1 — ping fails — so /healthz
    # must degrade to 503 while still reporting which dependency is down.
    resp = client.get("/healthz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["db"] is True and body["redis"] is False
