"""Dry-run simulation: caller-ID assignment, scheduling, block handling, CSV."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone

from conftest import make_settings

import orchestrator.compliance as compliance_pkg
import orchestrator.db as db
from orchestrator.dryrun import csv_out, plan
from orchestrator.models import BlockReason, DialDecision, TouchType

# 15:00 UTC = 11:00 EDT — inside the default 09:00–17:00 window for Ohio (614).
NOW = datetime(2026, 8, 10, 15, 0, tzinfo=timezone.utc)
TOMORROW = datetime(2026, 8, 11, 13, 0, tzinfo=timezone.utc)


def _contact(cid: int, phone: str, **extra) -> dict:
    row = {
        "id": cid,
        "justcall_contact_id": 1000 + cid,
        "name": f"Owner {cid}",
        "company_name": f"Co {cid}",
        "phone_e164": phone,
        "touch_type": "first",
        "progress_status": "Undialed",
        "contact_status": "Active",
        "consent_basis": "prior_express_consent",
    }
    row.update(extra)
    return row


def _number(nid: int, phone: str, area: str, *, dials_today: int = 0) -> dict:
    return {
        "id": nid,
        "phone_e164": phone,
        "area_code": area,
        "status": "active",
        "daily_cap": None,
        "purchased_at": NOW - timedelta(days=30),  # past the ramp: full cap
        "last_used_at": None,
        "dials_today": dials_today,
    }


def _patch_env(monkeypatch, contacts, numbers, decisions):
    def fake_query(cfg, sql, params=None):
        if "FROM contacts" in sql:
            return contacts
        if "FROM numbers" in sql:
            return numbers
        if "GROUP BY contact_id" in sql:
            return []
        raise AssertionError(f"unexpected query: {sql}")

    monkeypatch.setattr(db, "query", fake_query)
    monkeypatch.setattr(
        compliance_pkg, "check_dial_allowed",
        lambda cfg, contact, now=None: decisions[contact["id"]],
    )


def test_allowed_contact_gets_area_matched_caller_and_time(monkeypatch, tmp_path):
    cfg = make_settings()
    contacts = [_contact(1, "+16145550001")]
    numbers = [_number(1, "+16145559999", "614"), _number(2, "+18135558888", "813")]
    _patch_env(monkeypatch, contacts, numbers, {1: DialDecision(allowed=True)})

    rows = plan.build_dry_run(cfg, [TouchType.FIRST], now=NOW)
    assert len(rows) == 1
    row = rows[0]
    assert row.allowed and row.blocked_reasons == ""
    assert row.caller_id == "+16145559999" and row.caller_id_match == "area_code"
    assert row.would_dial_at is not None and row.would_dial_at >= NOW
    assert row.state == "OH"
    assert row.attempt_number == 1

    out = tmp_path / "plan.csv"
    csv_out.write_csv(rows, out)
    with out.open() as fh:
        parsed = list(csv.DictReader(fh))
    assert parsed[0]["caller_id"] == "+16145559999"
    assert parsed[0]["allowed"] == "true"
    assert list(parsed[0].keys()) == plan.FIELD_NAMES


def test_permanent_block_has_no_time_or_caller(monkeypatch):
    cfg = make_settings()
    contacts = [_contact(1, "+16145550001")]
    _patch_env(
        monkeypatch, contacts, [_number(1, "+16145559999", "614")],
        {1: DialDecision(allowed=False, reasons=[BlockReason.SUPPRESSED])},
    )
    (row,) = plan.build_dry_run(cfg, [TouchType.FIRST], now=NOW)
    assert not row.allowed
    assert row.blocked_reasons == "suppressed"
    assert row.would_dial_at is None and row.caller_id is None


def test_temporal_block_schedules_at_window_open(monkeypatch):
    cfg = make_settings()
    contacts = [_contact(1, "+16145550001")]
    decision = DialDecision(
        allowed=False, reasons=[BlockReason.OUTSIDE_WINDOW], earliest_allowed=TOMORROW
    )
    _patch_env(monkeypatch, contacts, [_number(1, "+16145559999", "614")], {1: decision})
    (row,) = plan.build_dry_run(cfg, [TouchType.FIRST], now=NOW)
    assert not row.allowed and "outside_window" in row.blocked_reasons
    assert row.would_dial_at is not None and row.would_dial_at >= TOMORROW
    assert row.caller_id == "+16145559999"


def test_empty_pool_marks_no_number_available(monkeypatch):
    cfg = make_settings()
    contacts = [_contact(1, "+16145550001")]
    _patch_env(monkeypatch, contacts, [], {1: DialDecision(allowed=True)})
    (row,) = plan.build_dry_run(cfg, [TouchType.FIRST], now=NOW)
    assert row.caller_id is None
    assert "no_number_available" in row.blocked_reasons
    assert row.would_dial_at is not None  # schedule shape still visible


def test_kill_switch_reason_is_stripped(monkeypatch):
    # The dry run plans; the switch halts execution. A fail-closed switch
    # (e.g. no Redis on the analyst's laptop) must not stamp every row.
    cfg = make_settings()
    contacts = [_contact(1, "+16145550001")]
    decision = DialDecision(allowed=False, reasons=[BlockReason.KILL_SWITCH])
    _patch_env(monkeypatch, contacts, [_number(1, "+16145559999", "614")], {1: decision})
    (row,) = plan.build_dry_run(cfg, [TouchType.FIRST], now=NOW)
    assert row.allowed and row.blocked_reasons == ""


def test_cpm_spread_over_minutes(monkeypatch):
    cfg = make_settings(calls_per_minute=1)
    contacts = [_contact(i, f"+161455500{i:02d}") for i in range(1, 4)]
    decisions = {c["id"]: DialDecision(allowed=True) for c in contacts}
    _patch_env(monkeypatch, contacts, [_number(1, "+16145559999", "614")], decisions)
    rows = plan.build_dry_run(cfg, [TouchType.FIRST], now=NOW)
    minutes = {r.would_dial_at.replace(second=0, microsecond=0) for r in rows}
    assert len(minutes) == 3  # one per minute at CPM=1
