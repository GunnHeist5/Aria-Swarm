"""pool.health.auto_bench: relative-threshold benching + one alert per action.

The stats query and the bench UPDATE are faked at the orchestrator.db level;
alerts are captured by monkeypatching queueing.alerts.send_alert (the real
module — health imports it lazily and resolves the attribute at call time).
"""

from __future__ import annotations

import pytest
from conftest import make_settings

import orchestrator.db
import orchestrator.queueing.alerts
from orchestrator.pool import health


@pytest.fixture
def harness(monkeypatch):
    state = {"rows": [], "benched": [], "alerts": []}

    def query(cfg, sql, params=None):
        assert "FROM numbers n" in sql
        return state["rows"]

    def execute(cfg, sql, params=None):
        assert "benched" in sql
        state["benched"].append(params)
        return 1

    monkeypatch.setattr(orchestrator.db, "query", query)
    monkeypatch.setattr(orchestrator.db, "execute", execute)
    monkeypatch.setattr(
        orchestrator.queueing.alerts, "send_alert",
        lambda cfg, text: state["alerts"].append(text),
    )
    return state


def stats_row(number_id: int, dials: int, connects: int) -> dict:
    return {
        "id": number_id,
        "phone_e164": f"+1614555{number_id:04d}",
        "dials": dials,
        "connects": connects,
    }


def test_benches_underperformer_with_alert(harness):
    cfg = make_settings(bench_min_dials=30, bench_relative_threshold=0.5)
    # Pool: 100 dials/30 connects + 50 dials/1 connect → avg 31/150 ≈ 0.2067.
    # Number 2's rate 0.02 < 0.5 × avg → benched.
    harness["rows"] = [stats_row(1, 100, 30), stats_row(2, 50, 1)]
    actions = health.auto_bench(cfg)
    assert [a.number_id for a in actions] == [2]
    assert actions[0].connect_rate == pytest.approx(0.02)
    assert actions[0].pool_avg == pytest.approx(31 / 150)
    assert [p["number_id"] for p in harness["benched"]] == [2]
    assert len(harness["alerts"]) == 1
    assert "+16145550002" in harness["alerts"][0]


def test_too_few_dials_is_not_judged(harness):
    cfg = make_settings(bench_min_dials=30)
    harness["rows"] = [stats_row(1, 100, 30), stats_row(2, 29, 0)]  # under min
    assert health.auto_bench(cfg) == []
    assert harness["benched"] == []
    assert harness["alerts"] == []


def test_healthy_number_not_benched(harness):
    cfg = make_settings(bench_min_dials=30, bench_relative_threshold=0.5)
    harness["rows"] = [stats_row(1, 100, 30), stats_row(2, 100, 28)]
    assert health.auto_bench(cfg) == []


def test_zero_connect_pool_produces_no_bench(harness):
    # No relative signal — benching everything would empty the pool.
    cfg = make_settings(bench_min_dials=10)
    harness["rows"] = [stats_row(1, 100, 0), stats_row(2, 100, 0)]
    assert health.auto_bench(cfg) == []
    assert harness["alerts"] == []


def test_zero_dials_pool_is_noop(harness):
    cfg = make_settings()
    harness["rows"] = [stats_row(1, 0, 0)]
    assert health.auto_bench(cfg) == []


def test_multiple_benches_get_one_alert_each(harness):
    cfg = make_settings(bench_min_dials=10, bench_relative_threshold=0.5)
    harness["rows"] = [
        stats_row(1, 100, 60),
        stats_row(2, 50, 1),
        stats_row(3, 40, 1),
    ]
    actions = health.auto_bench(cfg)
    assert [a.number_id for a in actions] == [2, 3]
    assert len(harness["alerts"]) == 2


def test_alert_failure_does_not_stop_benching(harness, monkeypatch):
    cfg = make_settings(bench_min_dials=10, bench_relative_threshold=0.5)
    harness["rows"] = [stats_row(1, 100, 60), stats_row(2, 50, 1)]

    def boom(cfg, text):
        raise ConnectionError("webhook down")

    monkeypatch.setattr(orchestrator.queueing.alerts, "send_alert", boom)
    actions = health.auto_bench(cfg)
    assert [a.number_id for a in actions] == [2]
    assert [p["number_id"] for p in harness["benched"]] == [2]
