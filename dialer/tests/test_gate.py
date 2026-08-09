"""check_dial_allowed: reason collection, phase gates, fail-closed kill switch.

The kill switch lives in orchestrator/queueing (a sibling agent's package),
imported lazily by the gate — tests install a fake module in sys.modules so
this suite runs with or without that package present. DB access goes through
suppression/attempts, so orchestrator.db is monkeypatched.
"""

from __future__ import annotations

import sys
import types
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from conftest import make_settings

import orchestrator.db
from orchestrator.compliance import check_dial_allowed
from orchestrator.models import BlockReason

UTC = timezone.utc
EASTERN = ZoneInfo("America/New_York")

PHONE = "+16145550100"                            # OH, Eastern
IN_WINDOW = datetime(2026, 1, 15, 15, 0, tzinfo=UTC)   # 10:00 EST
AFTER_CLOSE = datetime(2026, 1, 15, 23, 30, tzinfo=UTC)  # 18:30 EST


def contact_row(**overrides) -> dict:
    row = {
        "id": 1,
        "justcall_contact_id": 100,
        "name": "Pat Owner",
        "company_name": "Acme Drain Co",
        "phone_e164": PHONE,
        "contact_status": "Active",
        "progress_status": "Dialed",
        "touch_type": "second",
        "consent_basis": "established_business_relationship",
    }
    row.update(overrides)
    return row


@pytest.fixture
def killswitch(monkeypatch) -> dict:
    """Fake orchestrator.queueing.killswitch; toggle via state['engaged']."""
    state = {"engaged": False}
    fake = types.ModuleType("orchestrator.queueing.killswitch")
    fake.is_engaged = lambda cfg: state["engaged"]  # type: ignore[attr-defined]
    pkg = types.ModuleType("orchestrator.queueing")
    pkg.killswitch = fake  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "orchestrator.queueing", pkg)
    monkeypatch.setitem(sys.modules, "orchestrator.queueing.killswitch", fake)
    return state


@pytest.fixture
def fakedb(monkeypatch) -> dict:
    """suppressed set + per-phone attempt counts behind orchestrator.db."""
    state = {"suppressed": set(), "attempts": {}}

    def query_one(cfg, sql, params=None):
        if "FROM suppression" in sql:
            return {"present": 1} if params[0] in state["suppressed"] else None
        if "FROM call_attempts" in sql:
            return {"n": state["attempts"].get(params[0], 0)}
        raise AssertionError(f"unexpected query: {sql}")

    monkeypatch.setattr(orchestrator.db, "query_one", query_one)
    return state


@pytest.fixture
def live_cfg(tmp_path):
    """Phase-3 settings with a DNC file that does NOT list PHONE."""
    dnc = tmp_path / "dnc.txt"
    dnc.write_text("# fixture\n6145559999\n")
    return make_settings(phase=3, dnc_list_path=str(dnc))


# --- the happy path ---------------------------------------------------------

def test_fully_allowed(live_cfg, killswitch, fakedb):
    decision = check_dial_allowed(live_cfg, contact_row(), now=IN_WINDOW)
    assert decision.allowed
    assert decision.reasons == []
    assert decision.earliest_allowed is None


# --- phase gate -------------------------------------------------------------

def test_phase1_blocks_everything(killswitch, fakedb, tmp_path):
    dnc = tmp_path / "dnc.txt"
    dnc.write_text("")
    cfg = make_settings(phase=1, dnc_list_path=str(dnc))
    for touch in ("first", "second"):
        decision = check_dial_allowed(cfg, contact_row(touch_type=touch), now=IN_WINDOW)
        assert BlockReason.PHASE_GATE in decision.reasons
        assert decision.permanently_blocked


def test_phase2_blocks_first_touch_only(killswitch, fakedb, tmp_path):
    dnc = tmp_path / "dnc.txt"
    dnc.write_text("")
    cfg = make_settings(phase=2, dnc_list_path=str(dnc))
    first = check_dial_allowed(cfg, contact_row(touch_type="first"), now=IN_WINDOW)
    assert BlockReason.PHASE_GATE in first.reasons
    second = check_dial_allowed(cfg, contact_row(touch_type="second"), now=IN_WINDOW)
    assert second.allowed


def test_unknown_touch_type_fails_closed_in_phase2(killswitch, fakedb, tmp_path):
    dnc = tmp_path / "dnc.txt"
    dnc.write_text("")
    cfg = make_settings(phase=2, dnc_list_path=str(dnc))
    decision = check_dial_allowed(cfg, contact_row(touch_type="???"), now=IN_WINDOW)
    assert BlockReason.PHASE_GATE in decision.reasons


# --- kill switch ------------------------------------------------------------

def test_killswitch_blocks_temporally(live_cfg, killswitch, fakedb):
    killswitch["engaged"] = True
    decision = check_dial_allowed(live_cfg, contact_row(), now=IN_WINDOW)
    assert decision.reasons == [BlockReason.KILL_SWITCH]
    assert not decision.permanently_blocked
    assert decision.earliest_allowed is None  # no window block -> no reopen time


def test_killswitch_import_failure_is_engaged(live_cfg, fakedb, monkeypatch):
    # None in sys.modules makes the import raise: the gate must treat an
    # unconsultable kill switch as ENGAGED.
    monkeypatch.setitem(sys.modules, "orchestrator.queueing.killswitch", None)
    decision = check_dial_allowed(live_cfg, contact_row(), now=IN_WINDOW)
    assert BlockReason.KILL_SWITCH in decision.reasons


def test_killswitch_error_is_engaged(live_cfg, killswitch, fakedb, monkeypatch):
    def boom(cfg):
        raise ConnectionError("redis down")

    sys.modules["orchestrator.queueing.killswitch"].is_engaged = boom
    decision = check_dial_allowed(live_cfg, contact_row(), now=IN_WINDOW)
    assert BlockReason.KILL_SWITCH in decision.reasons


# --- per-check blocks -------------------------------------------------------

def test_contact_status_dnca_and_invalid(live_cfg, killswitch, fakedb):
    dnca = check_dial_allowed(live_cfg, contact_row(contact_status="DNCA"), now=IN_WINDOW)
    assert BlockReason.CONTACT_DNCA in dnca.reasons
    invalid = check_dial_allowed(live_cfg, contact_row(contact_status="Invalid"), now=IN_WINDOW)
    assert BlockReason.CONTACT_INVALID in invalid.reasons


def test_invalid_phone_blocks_and_skips_phone_checks(live_cfg, killswitch, fakedb):
    decision = check_dial_allowed(live_cfg, contact_row(phone_e164="garbage"), now=IN_WINDOW)
    assert BlockReason.INVALID_PHONE in decision.reasons
    assert decision.permanently_blocked
    # No phone -> no window/DNC/suppression reasons piled on top.
    assert BlockReason.OUTSIDE_WINDOW not in decision.reasons


def test_suppressed(live_cfg, killswitch, fakedb):
    fakedb["suppressed"].add(PHONE)
    decision = check_dial_allowed(live_cfg, contact_row(), now=IN_WINDOW)
    assert BlockReason.SUPPRESSED in decision.reasons
    assert decision.permanently_blocked


def test_no_consent_basis(live_cfg, killswitch, fakedb):
    decision = check_dial_allowed(live_cfg, contact_row(consent_basis=None), now=IN_WINDOW)
    assert BlockReason.NO_CONSENT_BASIS in decision.reasons


def test_dnc_source_missing(killswitch, fakedb):
    cfg = make_settings(phase=3, dnc_list_path=None)
    decision = check_dial_allowed(cfg, contact_row(), now=IN_WINDOW)
    assert BlockReason.DNC_SOURCE_MISSING in decision.reasons


def test_federal_dnc_listed(killswitch, fakedb, tmp_path):
    dnc = tmp_path / "dnc.txt"
    dnc.write_text("6145550100\n")  # PHONE's national number
    cfg = make_settings(phase=3, dnc_list_path=str(dnc))
    decision = check_dial_allowed(cfg, contact_row(), now=IN_WINDOW)
    assert BlockReason.FEDERAL_DNC in decision.reasons


def test_unknown_area_code(live_cfg, killswitch, fakedb):
    decision = check_dial_allowed(
        live_cfg, contact_row(phone_e164="+19995550100"), now=IN_WINDOW
    )
    assert BlockReason.UNKNOWN_AREA_CODE in decision.reasons
    # Unknown zone also reads as outside-window with no reopen time.
    assert BlockReason.OUTSIDE_WINDOW in decision.reasons
    assert decision.permanently_blocked


def test_non_us_number_blocked_by_default(live_cfg, killswitch, fakedb):
    decision = check_dial_allowed(
        live_cfg, contact_row(phone_e164="+14165550100"), now=IN_WINDOW  # Toronto
    )
    assert BlockReason.NON_US_NUMBER in decision.reasons


def test_non_us_number_allowed_when_configured(killswitch, fakedb, tmp_path):
    dnc = tmp_path / "dnc.txt"
    dnc.write_text("")
    cfg = make_settings(phase=3, dnc_list_path=str(dnc), allow_non_us_nanp=True)
    decision = check_dial_allowed(
        cfg, contact_row(phone_e164="+14165550100"), now=IN_WINDOW
    )
    assert decision.allowed  # Toronto is Eastern: same window as Ohio


def test_outside_window_sets_earliest_allowed(live_cfg, killswitch, fakedb):
    decision = check_dial_allowed(live_cfg, contact_row(), now=AFTER_CLOSE)
    assert decision.reasons == [BlockReason.OUTSIDE_WINDOW]
    assert not decision.permanently_blocked
    assert decision.earliest_allowed == datetime(2026, 1, 16, 9, 0, tzinfo=EASTERN)


def test_attempt_cap(live_cfg, killswitch, fakedb):
    fakedb["attempts"][PHONE] = live_cfg.max_attempts_total  # 4 >= 4
    decision = check_dial_allowed(live_cfg, contact_row(), now=IN_WINDOW)
    assert BlockReason.ATTEMPT_CAP in decision.reasons
    assert decision.permanently_blocked


def test_below_attempt_cap_allowed(live_cfg, killswitch, fakedb):
    fakedb["attempts"][PHONE] = live_cfg.max_attempts_total - 1
    assert check_dial_allowed(live_cfg, contact_row(), now=IN_WINDOW).allowed


# --- no short-circuit -------------------------------------------------------

def test_all_reasons_collected(killswitch, fakedb):
    # Phase 1 + suppressed + no consent + no DNC source + after close: the
    # decision must list every one, not stop at the first (SPEC §9 tallies).
    fakedb["suppressed"].add(PHONE)
    cfg = make_settings(phase=1, dnc_list_path=None)
    decision = check_dial_allowed(
        cfg, contact_row(consent_basis=None), now=AFTER_CLOSE
    )
    assert set(decision.reasons) == {
        BlockReason.PHASE_GATE,
        BlockReason.SUPPRESSED,
        BlockReason.NO_CONSENT_BASIS,
        BlockReason.DNC_SOURCE_MISSING,
        BlockReason.OUTSIDE_WINDOW,
    }
    assert decision.permanently_blocked
    assert decision.earliest_allowed is None  # permanent blocks: no retry time


def test_default_now_is_utc_now(live_cfg, killswitch, fakedb):
    # Only asserts it runs and returns a DialDecision — wall-clock dependent.
    decision = check_dial_allowed(live_cfg, contact_row())
    assert isinstance(decision.allowed, bool)


# --- consent config (compliance.consent, gate-adjacent) ---------------------

def test_consent_basis_for_reports_config_verbatim():
    from orchestrator.compliance.consent import consent_basis_for
    from orchestrator.models import TouchType

    cfg = make_settings(
        consent_basis_first_touch=None,
        consent_basis_second_touch="established_business_relationship",
    )
    # None means "not dialable", never a default.
    assert consent_basis_for(cfg, TouchType.FIRST) is None
    assert consent_basis_for(cfg, TouchType.SECOND) == "established_business_relationship"


def test_two_party_states_from_shipped_file():
    from orchestrator.compliance.consent import is_two_party_state

    cfg = make_settings()
    for state in ("CA", "fl", "WA", "Pa"):  # case-insensitive
        assert is_two_party_state(cfg, state)
    for state in ("OH", "TX", "NY"):
        assert not is_two_party_state(cfg, state)
    # Unknown state: apply the stricter policy (fail closed).
    assert is_two_party_state(cfg, None)


def test_two_party_states_missing_file_raises(tmp_path):
    from orchestrator.compliance.consent import is_two_party_state
    from orchestrator.config import ConfigError

    cfg = make_settings(two_party_states_path=str(tmp_path / "nope.json"))
    with pytest.raises(ConfigError):
        is_two_party_state(cfg, "CA")
