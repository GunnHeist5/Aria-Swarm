"""Planner: gate-driven scheduling, blocked tallies, phase-2 clamps."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from conftest import make_settings

import orchestrator.compliance as compliance_pkg
import orchestrator.db as db
from orchestrator.models import BlockReason, DialDecision, TouchType
from orchestrator.queueing import planner

NOW = datetime(2026, 8, 10, 15, 0, tzinfo=timezone.utc)


def _contact(cid: int) -> dict:
    return {
        "id": cid, "justcall_contact_id": 1000 + cid, "phone_e164": f"+16145550{cid:03d}",
        "touch_type": "second", "progress_status": "Dialed", "contact_status": "Active",
        "consent_basis": "prior_express_consent",
    }


def _wire(monkeypatch, contacts, decisions, *, live_second=0):
    inserted: list[dict] = []

    def fake_query(cfg, sql, params=None):
        assert "FROM contacts" in sql
        limit = params.get("limit")
        return contacts if limit is None else contacts[:limit]

    def fake_query_one(cfg, sql, params=None):
        if "MAX(ATTEMPT_NUMBER)" in sql.upper():
            return {"n": 1}  # the SQL computes MAX+1: this IS the next number
        if "touch_type = 'second'" in sql:
            return {"n": live_second}
        raise AssertionError(sql)

    def fake_execute(cfg, sql, params=None):
        assert "INSERT" in sql.upper()
        inserted.append(dict(params))
        return 1

    monkeypatch.setattr(db, "query", fake_query)
    monkeypatch.setattr(db, "query_one", fake_query_one)
    monkeypatch.setattr(db, "execute", fake_execute)
    monkeypatch.setattr(
        compliance_pkg, "check_dial_allowed",
        lambda cfg, contact, now=None: decisions[contact["id"]],
    )
    return inserted


def test_allowed_and_blocked_split(monkeypatch):
    contacts = [_contact(1), _contact(2), _contact(3)]
    decisions = {
        1: DialDecision(allowed=True),
        2: DialDecision(allowed=False, reasons=[BlockReason.SUPPRESSED]),
        3: DialDecision(
            allowed=False, reasons=[BlockReason.OUTSIDE_WINDOW],
            earliest_allowed=NOW + timedelta(hours=18),
        ),
    }
    inserted = _wire(monkeypatch, contacts, decisions)
    stats = planner.plan_touch(make_settings(phase=3), TouchType.SECOND, now=NOW)
    assert stats.considered == 3 and stats.scheduled == 2
    assert stats.blocked == {"suppressed": 1}
    whens = sorted(row["when"] for row in inserted)
    assert whens[0] >= NOW and whens[1] >= NOW + timedelta(hours=18)
    keys = {row["key"] for row in inserted}
    assert keys == {"contact:1:touch:second:attempt:1", "contact:3:touch:second:attempt:1"}


def test_temporal_block_without_reopen_is_skipped(monkeypatch):
    # Kill switch / unknown zone: no computable time means no scheduled guess.
    contacts = [_contact(1)]
    decisions = {1: DialDecision(allowed=False, reasons=[BlockReason.KILL_SWITCH])}
    inserted = _wire(monkeypatch, contacts, decisions)
    stats = planner.plan_touch(make_settings(phase=3), TouchType.SECOND, now=NOW)
    assert stats.scheduled == 0 and inserted == []
    assert stats.blocked == {"kill_switch": 1}


def test_phase2_refuses_first_touch(monkeypatch):
    inserted = _wire(monkeypatch, [], {})
    stats = planner.plan_touch(make_settings(phase=2), TouchType.FIRST, now=NOW)
    assert stats.considered == 0 and inserted == []


def test_phase2_clamps_second_touch_budget(monkeypatch):
    contacts = [_contact(i) for i in range(1, 6)]
    decisions = {c["id"]: DialDecision(allowed=True) for c in contacts}
    inserted = _wire(monkeypatch, contacts, decisions, live_second=48)
    cfg = make_settings(phase=2, phase2_max_contacts=50)
    stats = planner.plan_touch(cfg, TouchType.SECOND, now=NOW)
    # 48 live already, budget 2 — only two rows may be created.
    assert stats.scheduled == 2 and len(inserted) == 2


def test_cpm_spreads_schedule(monkeypatch):
    contacts = [_contact(i) for i in range(1, 5)]
    decisions = {c["id"]: DialDecision(allowed=True) for c in contacts}
    inserted = _wire(monkeypatch, contacts, decisions)
    cfg = make_settings(phase=3, calls_per_minute=2)
    planner.plan_touch(cfg, TouchType.SECOND, now=NOW)
    minutes = [row["when"].replace(second=0, microsecond=0) for row in inserted]
    assert len(set(minutes)) == 2  # 4 contacts at CPM=2 → two distinct minutes
