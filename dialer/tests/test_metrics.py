"""Report aggregation: derived rates and the rendered report."""

from __future__ import annotations

from conftest import make_settings

import orchestrator.db as db
from orchestrator.report import metrics


def _fake_db(monkeypatch):
    def fake_query_one(cfg, sql, params=None):
        if "FROM call_attempts" in sql:
            return {
                "dials": 10, "connects": 4, "bookings": 2, "opt_outs": 1,
                "failed": 1, "orphaned": 1, "scheduled": 5, "canceled": 3,
                "avg_duration_sec": 95.0, "total_cost_usd": 5.0,
            }
        if "FROM suppression" in sql:
            return {"n": 7}
        raise AssertionError(sql)

    def fake_query(cfg, sql, params=None):
        if "status = 'canceled'" in sql:
            return [{"error": "suppressed,outside_window"}, {"error": "suppressed"}]
        if "FROM numbers" in sql:
            return [{
                "id": 1, "phone_e164": "+16145559999", "area_code": "614",
                "status": "active", "benched_reason": None, "dials": 20, "connects": 5,
            }]
        if "FROM writeback_outbox" in sql:
            return [{"status": "pending", "n": 2}]
        raise AssertionError(sql)

    monkeypatch.setattr(db, "query_one", fake_query_one)
    monkeypatch.setattr(db, "query", fake_query)


def test_gather_and_render(monkeypatch):
    _fake_db(monkeypatch)
    cfg = make_settings()
    m = metrics.gather_metrics(cfg)
    assert m["connect_rate"] == 0.4
    assert m["error_rate"] == 0.2
    assert m["cost_per_dial_usd"] == 0.5
    assert m["cost_per_conversation_usd"] == 1.25
    assert m["cost_per_booking_usd"] == 2.5
    assert m["blocked"] == {"suppressed": 2, "outside_window": 1}
    assert m["numbers"][0]["connect_rate_7d"] == 0.25
    assert m["suppressed_total"] == 7

    text = metrics.render_report(m)
    assert "connect" in text and "40.0%" in text
    assert "$0.5000" in text and "suppressed" in text
    assert "+16145559999" in text


def test_zero_dials_has_no_rates(monkeypatch):
    def fake_query_one(cfg, sql, params=None):
        if "FROM call_attempts" in sql:
            return {"dials": 0, "connects": 0, "bookings": 0, "opt_outs": 0,
                    "failed": 0, "orphaned": 0, "scheduled": 0, "canceled": 0,
                    "avg_duration_sec": None, "total_cost_usd": 0}
        return {"n": 0}

    monkeypatch.setattr(db, "query_one", fake_query_one)
    monkeypatch.setattr(db, "query", lambda cfg, sql, params=None: [])
    m = metrics.gather_metrics(make_settings())
    assert m["connect_rate"] is None and m["cost_per_dial_usd"] is None
    assert "—" in metrics.render_report(m)
