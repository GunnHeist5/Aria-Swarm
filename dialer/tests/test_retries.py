"""Retry scheduling: exhaustion, caps, and window clamping."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from conftest import make_settings

import orchestrator.db as db
from orchestrator.compliance import attempts as compliance_attempts
from orchestrator.compliance import windows
from orchestrator.queueing import retries

NOW = datetime(2026, 8, 10, 15, 0, tzinfo=timezone.utc)

ROW = {
    "id": 9, "contact_id": 3, "touch_type": "second", "attempt_number": 1,
    "to_phone_e164": "+16145550001", "opener_variant": "opener_a",
    "consent_basis": "prior_express_consent",
}


def _wire(monkeypatch, *, attempts_so_far=1, window_open="passthrough"):
    inserted: list[dict] = []

    def fake_query_one(cfg, sql, params=None):
        if "MAX(ATTEMPT_NUMBER)" in sql.upper():
            return {"n": 2}
        if "INSERT" in sql.upper():
            inserted.append(dict(params))
            return {"id": 55}
        raise AssertionError(sql)

    monkeypatch.setattr(db, "query_one", fake_query_one)
    monkeypatch.setattr(
        compliance_attempts, "attempts_for_phone", lambda cfg, phone: attempts_so_far
    )
    if window_open == "passthrough":
        monkeypatch.setattr(windows, "next_window_open", lambda cfg, phone, at: at)
    else:
        monkeypatch.setattr(windows, "next_window_open", lambda cfg, phone, at: window_open)
    return inserted


def test_first_retry_uses_first_delay(monkeypatch):
    inserted = _wire(monkeypatch)
    cfg = make_settings(retry_schedule=["1d", "3d"])
    new_id = retries.schedule_retry(cfg, dict(ROW), now=NOW)
    assert new_id == 55
    assert inserted[0]["when"] == NOW + timedelta(days=1)
    assert inserted[0]["n"] == 2  # the MAX+1 query's value IS the next number
    assert inserted[0]["opener"] == "opener_a"  # A/B identity carries over


def test_schedule_exhausted_returns_none(monkeypatch):
    inserted = _wire(monkeypatch)
    cfg = make_settings(retry_schedule=["1d"])
    assert retries.schedule_retry(cfg, {**ROW, "attempt_number": 2}, now=NOW) is None
    assert inserted == []


def test_attempt_cap_blocks_retry(monkeypatch):
    inserted = _wire(monkeypatch, attempts_so_far=4)
    cfg = make_settings(max_attempts_total=4)
    assert retries.schedule_retry(cfg, dict(ROW), now=NOW) is None
    assert inserted == []


def test_unknown_window_means_no_retry(monkeypatch):
    inserted = _wire(monkeypatch, window_open=None)
    assert retries.schedule_retry(make_settings(), dict(ROW), now=NOW) is None
    assert inserted == []


def test_retry_clamped_into_window(monkeypatch):
    opens = NOW + timedelta(days=1, hours=3)
    inserted = _wire(monkeypatch, window_open=opens)
    retries.schedule_retry(make_settings(), dict(ROW), now=NOW)
    assert inserted[0]["when"] == opens
