"""pool.selector: ramp/cap arithmetic, cooldown, area-match round-robin, lease.

DB access is faked at the `db.tx` boundary: a FakeConn serves the candidates
query from prepared rows and records every write, so the tests prove exactly
which usage rows a lease touches without a live Postgres.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from conftest import make_settings

import orchestrator.db
from orchestrator.pool import selector

NOW = datetime(2026, 8, 5, 15, 0, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=30)   # fully ramped
TO_PHONE = "+16145550100"        # area code 614


def number_row(**overrides) -> dict:
    row = {
        "id": 1,
        "phone_e164": "+16145550001",
        "area_code": "614",
        "daily_cap": None,
        "purchased_at": OLD,
        "last_used_at": None,
        "dials_today": 0,
    }
    row.update(overrides)
    return row


class FakeConn:
    def __init__(self, rows: list[dict]):
        self.rows = rows
        self.writes: list[tuple[str, dict]] = []

    def execute(self, sql, params=None):
        if "FROM numbers n" in sql:
            return _Cursor(self.rows)
        self.writes.append((sql, params))
        return _Cursor([])


class _Cursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


def install_fake_tx(monkeypatch, rows: list[dict]) -> FakeConn:
    conn = FakeConn(rows)

    @contextmanager
    def fake_tx(cfg):
        yield conn

    monkeypatch.setattr(orchestrator.db, "tx", fake_tx)
    return conn


# --- ramp / cap arithmetic ---------------------------------------------------

def test_ramp_allowance_follows_schedule():
    cfg = make_settings()  # ramp [10, 25, 50, 100, 150]
    assert selector.ramp_allowance(cfg, 0) == 10
    assert selector.ramp_allowance(cfg, 1) == 25
    assert selector.ramp_allowance(cfg, 4) == 150
    # Beyond the schedule the ramp no longer constrains (None = full cap).
    assert selector.ramp_allowance(cfg, 5) is None
    assert selector.ramp_allowance(cfg, 400) is None


def test_effective_cap_is_min_of_cap_and_ramp():
    cfg = make_settings()  # per_number_daily_cap 150
    young = number_row(purchased_at=NOW - timedelta(days=1))  # age 1 → ramp 25
    assert selector.effective_daily_cap(cfg, young, now=NOW) == 25
    # Row-specific cap below the ramp allowance wins.
    capped = number_row(purchased_at=NOW - timedelta(days=1), daily_cap=5)
    assert selector.effective_daily_cap(cfg, capped, now=NOW) == 5
    # Beyond the schedule: the full cap applies, even a row cap above the
    # last ramp step.
    old_big = number_row(purchased_at=OLD, daily_cap=200)
    assert selector.effective_daily_cap(cfg, old_big, now=NOW) == 200
    assert selector.effective_daily_cap(cfg, number_row(), now=NOW) == 150


def test_missing_purchased_at_counts_as_brand_new():
    cfg = make_settings()
    row = number_row(purchased_at=None)
    assert selector.effective_daily_cap(cfg, row, now=NOW) == 10  # day-0 ramp


# --- pick_number selection ---------------------------------------------------

def test_exact_area_code_match_preferred(cfg, monkeypatch):
    rows = [
        number_row(id=1, area_code="212", phone_e164="+12125550001"),
        number_row(id=2, area_code="614", phone_e164="+16145550002"),
    ]
    install_fake_tx(monkeypatch, rows)
    lease = selector.pick_number(cfg, TO_PHONE, now=NOW)
    assert lease is not None
    assert lease.number_id == 2
    assert lease.exact_area_match is True


def test_round_robin_by_last_used_at(cfg, monkeypatch):
    rows = [
        number_row(id=1, last_used_at=NOW - timedelta(hours=1)),
        number_row(id=2, phone_e164="+16145550002", last_used_at=NOW - timedelta(hours=3)),
    ]
    install_fake_tx(monkeypatch, rows)
    lease = selector.pick_number(cfg, TO_PHONE, now=NOW)
    assert lease.number_id == 2  # least recently used first


def test_never_used_sorts_before_used(cfg, monkeypatch):
    rows = [
        number_row(id=1, last_used_at=NOW - timedelta(days=2)),
        number_row(id=2, phone_e164="+16145550002", last_used_at=None),
    ]
    install_fake_tx(monkeypatch, rows)
    assert selector.pick_number(cfg, TO_PHONE, now=NOW).number_id == 2


def test_round_robin_fallback_when_no_area_match(cfg, monkeypatch):
    rows = [number_row(id=1, area_code="212", phone_e164="+12125550001")]
    install_fake_tx(monkeypatch, rows)
    lease = selector.pick_number(cfg, TO_PHONE, now=NOW)
    assert lease.number_id == 1
    assert lease.exact_area_match is False


def test_daily_cap_skips_to_next_number(cfg, monkeypatch):
    rows = [
        number_row(id=1, dials_today=150),  # full cap spent
        number_row(id=2, phone_e164="+16145550002", dials_today=149),
    ]
    install_fake_tx(monkeypatch, rows)
    assert selector.pick_number(cfg, TO_PHONE, now=NOW).number_id == 2


def test_all_capped_returns_none_and_writes_nothing(cfg, monkeypatch):
    conn = install_fake_tx(monkeypatch, [number_row(dials_today=150)])
    assert selector.pick_number(cfg, TO_PHONE, now=NOW) is None
    assert conn.writes == []


def test_ramp_limits_young_number_below_full_cap(cfg, monkeypatch):
    # Age 0 → allowance 10; 10 dials today blocks it though the cap is 150.
    young = number_row(id=1, purchased_at=NOW - timedelta(hours=2), dials_today=10)
    fallback = number_row(id=2, area_code="212", phone_e164="+12125550001")
    install_fake_tx(monkeypatch, [young, fallback])
    lease = selector.pick_number(cfg, TO_PHONE, now=NOW)
    assert lease.number_id == 2
    assert lease.exact_area_match is False


def test_cooldown_skips_recently_used(monkeypatch):
    cfg = make_settings(number_cooldown_seconds=120)
    hot = number_row(id=1, last_used_at=NOW - timedelta(seconds=30))
    cool = number_row(id=2, phone_e164="+16145550002", last_used_at=NOW - timedelta(seconds=300))
    install_fake_tx(monkeypatch, [hot, cool])
    assert selector.pick_number(cfg, TO_PHONE, now=NOW).number_id == 2


def test_lease_increments_usage_and_last_used(cfg, monkeypatch):
    conn = install_fake_tx(monkeypatch, [number_row()])
    lease = selector.pick_number(cfg, TO_PHONE, now=NOW)
    assert lease is not None
    sqls = [sql for sql, _ in conn.writes]
    assert any("INSERT INTO number_usage" in s and "dials" in s for s in sqls)
    assert any("SET last_used_at" in s for s in sqls)
    usage_params = next(p for s, p in conn.writes if "INSERT INTO number_usage" in s)
    assert usage_params["number_id"] == 1
    assert usage_params["today"] == NOW.date()


# --- release / connect -------------------------------------------------------

def test_release_unused_decrements_guarded(cfg, monkeypatch):
    calls = []
    monkeypatch.setattr(
        orchestrator.db, "execute", lambda c, sql, params=None: calls.append((sql, params)) or 1
    )
    install_fake_tx(monkeypatch, [number_row()])
    lease = selector.pick_number(cfg, TO_PHONE, now=NOW)
    selector.release_unused(cfg, lease)
    sql, params = calls[-1]
    assert "GREATEST(dials - 1, 0)" in sql
    assert params["number_id"] == lease.number_id


def test_record_connect_upserts_connects(cfg, monkeypatch):
    calls = []
    monkeypatch.setattr(
        orchestrator.db, "execute", lambda c, sql, params=None: calls.append((sql, params)) or 1
    )
    selector.record_connect(cfg, 7)
    sql, params = calls[0]
    assert "connects" in sql
    assert params["number_id"] == 7
