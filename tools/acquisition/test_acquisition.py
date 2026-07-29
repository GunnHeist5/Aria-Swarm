"""Offline tests for the acquisition agent (M1) — all seams stubbed."""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import enroll as enroll_mod
from . import ledger
from .config import DEFAULT_CONFIG, config_hash, load_config


def _env_db(tmp_path: Path) -> None:
    os.environ["ACQUISITION_DB"] = str(tmp_path / "acquisition.db")
    os.environ["SUPPRESSION_STORE"] = str(tmp_path / "suppressed.json")


def _row(apn="0440240000280", email="jane@x.com", county="Harris", state="TX",
         **extra) -> dict:
    row = {"APN": apn, "County": county, "State": state,
           "Address": "1 Land Rd", "City": "Houston", "Zip": "77028",
           "Owner 1 First Name": "Jane", "Owner 1 Last Name": "Doe",
           "Email 1": email, "Lot Size Sqft": "21780",
           "Phone 1": "555-0101", "Phone 1 DNC": "Yes"}
    row.update(extra)
    return row


# ---------------------------------------------------------------------------
# ingest + dedup
# ---------------------------------------------------------------------------


def test_reingest_creates_zero_duplicates(tmp_path):
    _env_db(tmp_path)
    rows = [_row(), _row(apn="123", email="b@x.com")]
    r1 = ledger.ingest_rows(rows, source_list="export_a")
    r2 = ledger.ingest_rows(rows, source_list="export_a")
    assert r1["inserted"] == 2
    assert r2["inserted"] == 0 and r2["duplicate"] == 2


def test_ingest_no_key_rows_counted_not_crashed(tmp_path):
    _env_db(tmp_path)
    r = ledger.ingest_rows([{"County": "Harris"}], source_list="x")
    assert r["no_key"] == 1 and r["inserted"] == 0


def test_missing_apn_keys_on_address_and_still_dedups(tmp_path):
    _env_db(tmp_path)
    rows = [_row(apn="")]
    assert ledger.ingest_rows(rows, source_list="a")["inserted"] == 1
    assert ledger.ingest_rows(rows, source_list="b")["duplicate"] == 1


def test_dnc_flags_ride_through(tmp_path):
    _env_db(tmp_path)
    ledger.ingest_rows([_row()], source_list="x")
    conn = ledger.connect()
    phones = json.loads(conn.execute("SELECT phones FROM leads").fetchone()[0])
    conn.close()
    assert phones == [{"number": "555-0101", "dnc": True}]


def test_mark_enrolled_historical_import(tmp_path):
    _env_db(tmp_path)
    rows = [_row(), _row(apn="999", email="")]  # second has no email
    ledger.ingest_rows(rows, source_list="x", mark_enrolled=True)
    conn = ledger.connect()
    by_apn = {r["apn"]: r["status"] for r in conn.execute(
        "SELECT apn, status FROM leads")}
    conn.close()
    assert by_apn["0440240000280"] == "enrolled"   # had email -> was pushed
    assert by_apn["999"] == "new"                  # no email -> never pushed


# ---------------------------------------------------------------------------
# status machine + suppression
# ---------------------------------------------------------------------------


def test_status_history_records_transitions(tmp_path):
    _env_db(tmp_path)
    ledger.ingest_rows([_row()], source_list="x")
    ledger.set_status("harris_tx", "0440240000280", "negotiating", note="call")
    conn = ledger.connect()
    row = conn.execute("SELECT status, status_history FROM leads").fetchone()
    conn.close()
    history = json.loads(row["status_history"])
    assert row["status"] == "negotiating"
    assert [h["to"] for h in history] == ["new", "negotiating"]


def test_unknown_status_rejected(tmp_path):
    _env_db(tmp_path)
    ledger.ingest_rows([_row()], source_list="x")
    try:
        ledger.set_status("harris_tx", "0440240000280", "vibing")
        assert False
    except ledger.LedgerError:
        pass


def test_stop_suppression_flips_lead_and_writes_legacy_store(tmp_path):
    _env_db(tmp_path)
    ledger.ingest_rows([_row()], source_list="x")
    flipped = ledger.suppress_value("email", "Jane@X.com", reason="STOP")
    assert flipped == 1
    conn = ledger.connect()
    assert conn.execute("SELECT status FROM leads").fetchone()[0] == "suppressed"
    conn.close()
    legacy = json.loads(Path(os.environ["SUPPRESSION_STORE"]).read_text())
    assert "jane@x.com" in legacy                 # CAN-SPAM write-through


# ---------------------------------------------------------------------------
# enrollment gates (the non-negotiables)
# ---------------------------------------------------------------------------


def test_enrolling_negotiating_lead_hard_fails(tmp_path):
    _env_db(tmp_path)
    ledger.ingest_rows([_row()], source_list="x")
    ledger.set_status("harris_tx", "0440240000280", "negotiating")
    conn = ledger.connect()
    row = conn.execute("SELECT * FROM leads").fetchone()
    conn.close()
    # excluded from the eligible query...
    assert enroll_mod.eligible_leads(include_unscreened=True) == []
    # ...AND a hard failure if anything tries anyway
    try:
        ledger.assert_enrollable(row, set())
        assert False, "negotiating lead must hard-fail enrollment"
    except ledger.LedgerError as exc:
        assert "HARD STOP" in str(exc)


def test_enrolling_suppressed_email_hard_fails(tmp_path):
    _env_db(tmp_path)
    ledger.ingest_rows([_row()], source_list="x")
    conn = ledger.connect()
    row = conn.execute("SELECT * FROM leads").fetchone()
    conn.close()
    try:
        ledger.assert_enrollable(row, {"jane@x.com"})
        assert False
    except ledger.LedgerError:
        pass


def test_verdict_gate_default_excludes_unscreened(tmp_path):
    _env_db(tmp_path)
    ledger.ingest_rows([_row()], source_list="x")
    assert enroll_mod.eligible_leads() == []                       # strict default
    assert len(enroll_mod.eligible_leads(include_unscreened=True)) == 1
    ledger.write_enrichment("harris_tx", "0440240000280",
                            verdict="NEGOTIATE", mao=12000, open_at=10200,
                            walk_at=14000)
    assert len(enroll_mod.eligible_leads()) == 1                   # now eligible


def test_killed_verdict_never_eligible(tmp_path):
    _env_db(tmp_path)
    ledger.ingest_rows([_row()], source_list="x")
    ledger.write_enrichment("harris_tx", "0440240000280",
                            verdict="PASS", mao=None, open_at=None, walk_at=None)
    assert enroll_mod.eligible_leads(include_unscreened=True) == []


def test_enroll_payload_carries_mandatory_fields(tmp_path):
    _env_db(tmp_path)
    ledger.ingest_rows([_row()], source_list="harris_pull_1")
    ledger.write_enrichment("harris_tx", "0440240000280",
                            verdict="NEGOTIATE", mao=12000, open_at=10200,
                            walk_at=14000)
    row = enroll_mod.eligible_leads()[0]
    payload = enroll_mod.build_payload(row)
    custom = payload["custom_variables"]
    for key in ("apn", "county", "source_list", "verdict", "mao",
                "open_at", "walk_at"):
        assert key in custom, f"mandatory field {key} missing"
    assert custom["apn"] == "0440240000280" and custom["mao"] == 12000
    assert custom["propertyCity"] == "Houston"     # template vars still work


def test_enroll_suppression_is_last_gate_and_push_marks_enrolled(tmp_path):
    _env_db(tmp_path)
    ledger.ingest_rows([_row(), _row(apn="222", email="late@x.com")],
                       source_list="x")
    for apn in ("0440240000280", "222"):
        ledger.write_enrichment("harris_tx", apn, verdict="NEGOTIATE",
                                mao=1, open_at=1, walk_at=1)
    # suppression lands AFTER screening, right before the batch:
    ledger.suppress_value("email", "late@x.com", reason="STOP")

    posted = []

    def fake_post(url, payload, key):
        posted.append(payload["email"])
        return 200, "{}"

    report = enroll_mod.enroll(DEFAULT_CONFIG, push=True, limit=10,
                               campaign_id="camp-1", api_key="k",
                               http_post=fake_post, sleep=lambda _s: None,
                               log=lambda *_a: None)
    assert posted == ["jane@x.com"]                # suppressed lead never posted
    assert report["suppressed_last_gate"] == 0     # it was already status=suppressed
    conn = ledger.connect()
    status = conn.execute(
        "SELECT status FROM leads WHERE apn='0440240000280'").fetchone()[0]
    conn.close()
    assert status == "enrolled"                    # per-lead outcome recorded


def test_enroll_dry_run_touches_nothing(tmp_path):
    _env_db(tmp_path)
    ledger.ingest_rows([_row()], source_list="x")
    ledger.write_enrichment("harris_tx", "0440240000280", verdict="NEGOTIATE",
                            mao=1, open_at=1, walk_at=1)
    report = enroll_mod.enroll(DEFAULT_CONFIG, push=False, limit=10,
                               campaign_id="camp-1",
                               http_post=lambda *a: (_ for _ in ()).throw(
                                   AssertionError("network in dry-run")),
                               log=lambda *_a: None)
    assert report["dry_run"] and report["enrolled"] == 0
    conn = ledger.connect()
    assert conn.execute("SELECT status FROM leads").fetchone()[0] == "screened"
    conn.close()


# ---------------------------------------------------------------------------
# quota + config
# ---------------------------------------------------------------------------


def test_quota_accounting_accumulates_per_month(tmp_path):
    _env_db(tmp_path)
    assert ledger.quota_record(1000, month="2026-07") == 1000
    assert ledger.quota_record(500, month="2026-07") == 1500
    assert ledger.quota_record(200, month="2026-08") == 200
    assert ledger.quota_used("2026-07") == 1500


def test_config_overlay_and_unknown_key_rejected(tmp_path):
    p = tmp_path / "acq.yaml"
    p.write_text("quota_monthly: 25000\n", encoding="utf-8")
    cfg = load_config(str(p))
    assert cfg.quota_monthly == 25000
    assert config_hash(cfg) != config_hash(DEFAULT_CONFIG)
    p.write_text("nope: 1\n", encoding="utf-8")
    try:
        load_config(str(p))
        assert False
    except ValueError as exc:
        assert "nope" in str(exc)


def test_autonomy_flags_default_off():
    assert DEFAULT_CONFIG.auto_send_offers is False
    assert DEFAULT_CONFIG.graduated_autonomy is False


if __name__ == "__main__":
    import sys
    import tempfile

    failures = 0
    module = sys.modules[__name__]
    for name in sorted(dir(module)):
        if name.startswith("test_"):
            fn = getattr(module, name)
            code = fn.__code__
            try:
                if "tmp_path" in code.co_varnames[: code.co_argcount]:
                    with tempfile.TemporaryDirectory() as td:
                        fn(Path(td))
                else:
                    fn()
                print(f"ok {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {exc}")
    raise SystemExit(1 if failures else 0)


def test_skiptrace_reingest_backfills_contacts_and_unlocks_enrollment(tmp_path):
    """The live pull order: pull a county BEFORE skip trace (no contacts),
    then re-ingest the traced export. The second ingest must fill the
    contact fields — including the primary `email` that enrollment reads —
    without creating duplicates or touching status.

    Regression: the guard tested `emails IS NULL` while a contact-less
    ingest stores '[]', and only emails/email2 were written, never `email`.
    4,094 traced Harris emails stayed unenrollable because of it.
    """

    _env_db(tmp_path)
    # 1. pre-trace pull: no email, no phone
    pre = _row(email="", **{"Phone 1": "", "Phone 1 DNC": ""})
    r1 = ledger.ingest_rows([pre], source_list="harris_pull")
    assert r1["inserted"] == 1
    assert not enroll_mod.eligible_leads(include_unscreened=True), \
        "a contact-less lead must not be enrollable"

    # 2. traced re-export of the SAME parcel, now carrying contacts
    traced = _row(email="owner@x.com", **{"Email 2": "alt@x.com",
                                          "Phone 1": "555-0199"})
    r2 = ledger.ingest_rows([traced], source_list="harris_traced")
    assert r2["inserted"] == 0 and r2["duplicate"] == 1, "no duplicate rows"
    assert r2.get("contacts_backfilled") == 1

    rows = enroll_mod.eligible_leads(include_unscreened=True)
    assert [r["email"] for r in rows] == ["owner@x.com"], \
        "traced contacts must make the existing lead enrollable"
    lead = ledger.find_leads(email="owner@x.com")[0]
    assert lead["status"] == "new", "backfill must never move status"
    assert "alt@x.com" in (lead["emails"] or "")
    assert "555-0199" in (lead["phones"] or "")


def test_backfill_never_overwrites_existing_contacts(tmp_path):
    """First ingest wins for data that is already there."""

    _env_db(tmp_path)
    ledger.ingest_rows([_row(email="first@x.com")], source_list="a")
    ledger.ingest_rows([_row(email="second@x.com")], source_list="b")
    lead = ledger.find_leads(email="first@x.com")
    assert lead and lead[0]["email"] == "first@x.com"
    assert not ledger.find_leads(email="second@x.com")


def test_approve_falls_back_to_a_known_mailbox(tmp_path):
    """A hand-entered lead has no eaccount, which blocked its offer bumps
    outright. Fall back to the mailbox this account demonstrably sends
    from (the most recent queue item that has one)."""

    _env_db(tmp_path)
    from . import review as review_mod

    ledger.ingest_rows([_row()], source_list="x")
    conn = ledger.connect()
    ledger.init_db(conn)
    # a real reply carrying the mailbox...
    conn.execute(
        "INSERT INTO review_queue (id, county_key, apn, lead_email, eaccount, "
        "subject, reply_text, classification, draft, draft_kind, state, ts, "
        "created_at, updated_at) VALUES "
        "('real','harris_tx','0440240000280','s@x.com','me@aria.com','Lot',"
        "'hi','question','d','reply','sent','1','1','1')")
    # ...and a bump with none (the hand-entered lead)
    conn.execute(
        "INSERT INTO review_queue (id, county_key, apn, lead_email, eaccount, "
        "subject, reply_text, classification, draft, draft_kind, state, ts, "
        "reply_to, created_at, updated_at) VALUES "
        "('bump-x','harris_tx','0440240000280','s@x.com','','Lot','','bump',"
        "'circling back','bump','pending_review','2','real','2','2')")
    conn.commit()

    sent = {}

    def fake_http(method, url, payload, key):
        sent.update(payload)
        return 200, "{}"

    out = review_mod.approve("bump-x", send=True, http_request=fake_http,
                             conn=conn, log=lambda *_: None)
    conn.close()
    assert out["sent"] is True
    assert sent["eaccount"] == "me@aria.com", "must reuse a known mailbox"
    assert sent["reply_to_uuid"] == "real", "and still thread under the reply"
