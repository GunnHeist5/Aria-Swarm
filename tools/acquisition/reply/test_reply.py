"""Offline tests for the reply agent (M2) — all seams stubbed."""

from __future__ import annotations

import json
import os
from pathlib import Path

from .. import ledger, review
from ..config import DEFAULT_CONFIG
from . import classify as classify_mod
from . import draft as draft_mod
from . import ingest as ingest_mod
from . import verify as verify_mod


def _env_db(tmp_path: Path) -> None:
    os.environ["ACQUISITION_DB"] = str(tmp_path / "acq.db")
    os.environ["SUPPRESSION_STORE"] = str(tmp_path / "sup.json")


def _seed_lead(email="jane@x.com", apn="0440240000280", **enrich):
    ledger.ingest_rows([{
        "APN": apn, "County": "Harris", "State": "TX",
        "Address": "1 Land Rd", "City": "Houston", "Zip": "77028",
        "Owner 1 First Name": "Jane", "Owner 1 Last Name": "Doe",
        "Email 1": email, "Lot Size Sqft": "21780",
    }], source_list="t", mark_enrolled=True)
    if enrich:
        ledger.write_enrichment("harris_tx", apn, **enrich)


def _reply(id="em-1", email="jane@x.com", text="make me an offer",
           eaccount="justin@ariacap.com"):
    return {"id": id, "lead_email": email, "reply_text": text,
            "ts": "2026-07-22T12:00:00", "subject": "Your land in Harris",
            "eaccount": eaccount}


class _LLM:
    """Stub LLM: returns a queued response per invoke."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.prompts = []

    def invoke(self, prompt):
        self.prompts.append(prompt)
        return self.responses.pop(0) if self.responses else "{}"


# ---------------------------------------------------------------------------
# ingest + matching
# ---------------------------------------------------------------------------


def test_reply_matches_ledger_and_sets_replied(tmp_path):
    _env_db(tmp_path)
    _seed_lead()
    conn = ledger.connect()
    state = ingest_mod.ingest_reply(_reply(), conn=conn)
    assert state == "new"
    lead = conn.execute("SELECT status FROM leads").fetchone()
    item = conn.execute("SELECT * FROM review_queue").fetchone()
    conn.close()
    assert lead["status"] == "replied"
    assert item["county_key"] == "harris_tx" and item["apn"] == "0440240000280"


def test_unmatched_reply_goes_needs_manual_never_guesses_apn(tmp_path):
    _env_db(tmp_path)
    _seed_lead()
    conn = ledger.connect()
    state = ingest_mod.ingest_reply(_reply(email="stranger@y.com"), conn=conn)
    item = conn.execute("SELECT * FROM review_queue").fetchone()
    conn.close()
    assert state == "needs_manual"
    assert item["apn"] is None and item["county_key"] is None


def test_reingest_same_reply_is_duplicate(tmp_path):
    _env_db(tmp_path)
    _seed_lead()
    conn = ledger.connect()
    assert ingest_mod.ingest_reply(_reply(), conn=conn) == "new"
    assert ingest_mod.ingest_reply(_reply(), conn=conn) == "duplicate"
    conn.close()


def test_reply_never_regresses_mid_deal_status(tmp_path):
    _env_db(tmp_path)
    _seed_lead()
    ledger.set_status("harris_tx", "0440240000280", "offer_out")
    conn = ledger.connect()
    ingest_mod.ingest_reply(_reply(), conn=conn)
    status = conn.execute("SELECT status FROM leads").fetchone()[0]
    conn.close()
    assert status == "offer_out"          # not knocked back to 'replied'


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------


def test_stop_reply_auto_suppresses_and_removes(tmp_path):
    _env_db(tmp_path)
    _seed_lead()
    conn = ledger.connect()
    ingest_mod.ingest_reply(_reply(text="STOP"), conn=conn)
    removed = []
    report = classify_mod.classify_pending(
        conn=conn, llm=_LLM(),  # regex path — LLM never invoked
        remove_from_campaign=lambda e: removed.append(e) or True,
        log=lambda *_: None)
    item = conn.execute("SELECT state FROM review_queue").fetchone()[0]
    lead = conn.execute("SELECT status FROM leads").fetchone()[0]
    conn.close()
    assert report["stops"] == 1 and removed == ["jane@x.com"]
    assert item == "suppressed" and lead == "suppressed"
    legacy = json.loads(Path(os.environ["SUPPRESSION_STORE"]).read_text())
    assert "jane@x.com" in legacy


def test_stop_from_unmatched_sender_still_suppresses(tmp_path):
    _env_db(tmp_path)
    _seed_lead()
    conn = ledger.connect()
    ingest_mod.ingest_reply(_reply(id="em-9", email="stranger@y.com",
                                   text="unsubscribe"), conn=conn)
    report = classify_mod.classify_pending(conn=conn, llm=_LLM(),
                                           log=lambda *_: None)
    state = conn.execute(
        "SELECT state FROM review_queue WHERE id='em-9'").fetchone()[0]
    conn.close()
    assert report["stops"] == 1 and state == "suppressed"
    legacy = json.loads(Path(os.environ["SUPPRESSION_STORE"]).read_text())
    assert "stranger@y.com" in legacy      # CAN-SPAM: suppressed even unmatched


def test_garbage_llm_output_fails_closed_to_needs_manual(tmp_path):
    _env_db(tmp_path)
    _seed_lead()
    conn = ledger.connect()
    ingest_mod.ingest_reply(_reply(text="hmm interesting"), conn=conn)
    report = classify_mod.classify_pending(conn=conn, llm=_LLM("lol no json"),
                                           log=lambda *_: None)
    state = conn.execute("SELECT state FROM review_queue").fetchone()[0]
    conn.close()
    assert report["needs_manual"] == 1 and state == "needs_manual"


def test_price_given_classification_parses_price(tmp_path):
    result = classify_mod.classify_text(
        "I'd take $25,000 for it",
        llm=_LLM('{"classification": "price_given", "price_mentioned": 25000,'
                 ' "summary": "named 25k"}'))
    assert result["classification"] == "price_given"
    assert result["price_mentioned"] == 25000.0


# ---------------------------------------------------------------------------
# verification + offer box
# ---------------------------------------------------------------------------


def test_offer_box_math_table():
    box = verify_mod.offer_box(40_000, DEFAULT_CONFIG)
    assert box == {"retail": 40000.0, "buyer_ceiling": 30000.0,
                   "mao": 20000.0, "open_at": 17000.0, "walk_at": 25000.0}


def test_verify_fresh_screener_lead_is_verified(tmp_path):
    _env_db(tmp_path)
    _seed_lead(verdict="NEGOTIATE", mao=20000, open_at=17000, walk_at=25000,
               retail_estimate=40000, evidence={"comps": ["a", "b"]})
    conn = ledger.connect()
    v = verify_mod.verify_lead("harris_tx", "0440240000280", conn=conn)
    conn.close()
    assert v["verified"] and v["offer_box"]["mao"] == 20000.0
    assert v["confidence"] == "standard"


def test_verify_unscreened_lead_is_not_verified_and_rerun_failure_never_guesses(tmp_path):
    _env_db(tmp_path)
    _seed_lead()  # no enrichment
    conn = ledger.connect()
    v1 = verify_mod.verify_lead("harris_tx", "0440240000280", conn=conn)
    assert not v1["verified"]
    v2 = verify_mod.verify_lead(
        "harris_tx", "0440240000280", conn=conn,
        rerun=lambda c, a: (_ for _ in ()).throw(RuntimeError("gis down")),
        log=lambda *_: None)
    conn.close()
    assert not v2["verified"] and "re-enrichment failed" in v2["reason"]


def test_verify_rerun_seam_repairs_stale_lead(tmp_path):
    _env_db(tmp_path)
    _seed_lead()
    conn = ledger.connect()

    def rerun(county, apn):
        ledger.write_enrichment(county, apn, verdict="NEGOTIATE", mao=20000,
                                open_at=17000, walk_at=25000,
                                retail_estimate=40000, conn=conn)

    v = verify_mod.verify_lead("harris_tx", "0440240000280", conn=conn,
                               rerun=rerun, log=lambda *_: None)
    conn.close()
    assert v["verified"] and v["offer_box"]["open_at"] == 17000.0


# ---------------------------------------------------------------------------
# drafting — the number guards
# ---------------------------------------------------------------------------


def _through_verify(conn, llm_cls):
    classify_mod.classify_pending(conn=conn, llm=llm_cls, log=lambda *_: None)
    verify_mod.verify_classified(conn=conn, log=lambda *_: None)


def test_verified_offer_draft_has_open_at_and_decision_footer(tmp_path):
    _env_db(tmp_path)
    _seed_lead(verdict="NEGOTIATE", mao=20000, open_at=17000, walk_at=25000,
               retail_estimate=40000, evidence={"comps": ["77028 sale"]})
    conn = ledger.connect()
    ingest_mod.ingest_reply(_reply(), conn=conn)
    _through_verify(conn, _LLM('{"classification": "interested_no_price", '
                               '"price_mentioned": null, "summary": "s"}'))
    draft_llm = _LLM("Happy to make this simple: I can offer $17,000 cash, "
                     "as-is, we pay all costs.\n\nJustin Young\nARIA Capital LLC")
    report = draft_mod.draft_verified(conn=conn, llm=draft_llm,
                                      log=lambda *_: None)
    item = conn.execute("SELECT * FROM review_queue").fetchone()
    conn.close()
    assert report["drafted"] == 1
    assert item["state"] == "pending_review" and item["draft_kind"] == "offer"
    assert "$17,000" in item["draft"]
    assert "SIGN below:      $20,000" in item["draft"]      # decision footer
    assert "WALK above:      $25,000" in item["draft"]
    assert "reply STOP" in item["draft"]                    # compliance footer
    assert "playbook" in draft_llm.prompts[0].lower()       # rules were loaded


def test_unverified_lead_gets_holding_draft_with_no_number(tmp_path):
    _env_db(tmp_path)
    _seed_lead()  # never screened
    conn = ledger.connect()
    ingest_mod.ingest_reply(_reply(), conn=conn)
    _through_verify(conn, _LLM('{"classification": "interested_no_price", '
                               '"price_mentioned": null, "summary": "s"}'))
    report = draft_mod.draft_verified(
        conn=conn,
        llm=_LLM("SHOULD NEVER BE CALLED"),
        log=lambda *_: None)
    item = conn.execute("SELECT * FROM review_queue").fetchone()
    conn.close()
    assert report["holding"] == 1 and item["draft_kind"] == "holding"
    assert draft_mod.dollars_in(item["draft"]) == []        # zero $ figures
    assert "firm, written offer" in item["draft"]


def test_out_of_box_number_is_guard_rejected(tmp_path):
    _env_db(tmp_path)
    _seed_lead(verdict="NEGOTIATE", mao=20000, open_at=17000, walk_at=25000,
               retail_estimate=40000)
    conn = ledger.connect()
    ingest_mod.ingest_reply(_reply(), conn=conn)
    _through_verify(conn, _LLM('{"classification": "price_given", '
                               '"price_mentioned": 30000, "summary": "s"}'))
    report = draft_mod.draft_verified(
        conn=conn, llm=_LLM("I can do $28,000 for it."),  # above MAO!
        log=lambda *_: None)
    item = conn.execute("SELECT state, reason FROM review_queue").fetchone()
    conn.close()
    assert report["guard_rejected"] == 1
    assert item["state"] == "needs_manual" and "out-of-box" in item["reason"]


def test_counter_between_open_and_mao_is_allowed():
    box = {"open_at": 17000.0, "mao": 20000.0}
    assert draft_mod.numbers_allowed([18500.0], box)
    assert not draft_mod.numbers_allowed([20500.0], box)
    assert not draft_mod.numbers_allowed([16000.0], box)
    assert draft_mod.dollars_in("I can do $18.5k today") == [18500.0]


def test_wrong_person_never_gets_a_draft(tmp_path):
    _env_db(tmp_path)
    _seed_lead(verdict="NEGOTIATE", mao=20000, open_at=17000, walk_at=25000,
               retail_estimate=40000)
    conn = ledger.connect()
    ingest_mod.ingest_reply(_reply(text="Was already sold"), conn=conn)
    _through_verify(conn, _LLM('{"classification": "wrong_person", '
                               '"price_mentioned": null, "summary": "sold"}'))
    report = draft_mod.draft_verified(conn=conn,
                                      llm=_LLM("SHOULD NEVER BE CALLED"),
                                      log=lambda *_: None)
    item = conn.execute("SELECT state, reason, draft FROM review_queue").fetchone()
    conn.close()
    assert report.get("not_draftable") == 1
    assert item["state"] == "needs_manual" and item["draft"] is None
    assert "wrong_person" in item["reason"]


def test_reply_from_third_export_email_matches(tmp_path):
    _env_db(tmp_path)
    ledger.ingest_rows([{
        "APN": "777", "County": "Harris", "State": "TX",
        "Address": "2 Land Rd", "City": "Houston", "Zip": "77028",
        "Email 1": "primary@x.com", "Email 2": "second@x.com",
        "Email 3": "personal@gmail.com", "Lot Size Sqft": "21780",
    }], source_list="t", mark_enrolled=True)
    conn = ledger.connect()
    state = ingest_mod.ingest_reply(
        _reply(id="em-3", email="personal@gmail.com"), conn=conn)
    item = conn.execute("SELECT apn FROM review_queue").fetchone()
    conn.close()
    assert state == "new" and item["apn"] == "777"
    # and a STOP from that address flips the lead too
    assert ledger.suppress_value("email", "personal@gmail.com",
                                 reason="STOP") == 1


# ---------------------------------------------------------------------------
# review queue
# ---------------------------------------------------------------------------


def _queued_draft(tmp_path, kind="offer"):
    _env_db(tmp_path)
    _seed_lead(verdict="NEGOTIATE", mao=20000, open_at=17000, walk_at=25000,
               retail_estimate=40000)
    conn = ledger.connect()
    ingest_mod.ingest_reply(_reply(), conn=conn)
    conn.execute(
        "UPDATE review_queue SET state='pending_review', draft_kind=?, "
        "draft=? WHERE id='em-1'",
        (kind, "Offer text $17,000." + draft_mod.COMPLIANCE_FOOTER +
         "\n\n── approval guide ──\nSIGN below: $20,000"))
    conn.commit()
    return conn


def test_approve_without_send_is_dry_run(tmp_path):
    conn = _queued_draft(tmp_path)
    result = review.approve("em-1", send=False, conn=conn,
                            http_request=lambda *a: (_ for _ in ()).throw(
                                AssertionError("network on dry-run")),
                            log=lambda *_: None)
    state = conn.execute("SELECT state FROM review_queue").fetchone()[0]
    conn.close()
    assert result["dry_run"] and state == "pending_review"


def test_approve_send_strips_footer_threads_reply_and_advances_lead(tmp_path):
    conn = _queued_draft(tmp_path)
    sent = {}

    def fake_request(method, url, payload, key):
        sent.update(payload, _url=url, _method=method)
        return 200, "{}"

    result = review.approve("em-1", send=True, api_key="k", conn=conn,
                            http_request=fake_request, log=lambda *_: None)
    item = conn.execute("SELECT state FROM review_queue").fetchone()[0]
    lead = conn.execute("SELECT status FROM leads").fetchone()[0]
    conn.close()
    assert result["sent"] and item == "sent"
    assert lead == "offer_out"                               # offer draft
    assert sent["reply_to_uuid"] == "em-1"
    assert sent["eaccount"] == "justin@ariacap.com"
    assert sent["subject"].startswith("Re: ")
    assert "approval guide" not in sent["body"]["text"]      # footer stripped
    assert "reply STOP" in sent["body"]["text"]              # compliance stays


def test_approve_send_failure_keeps_item_pending(tmp_path):
    conn = _queued_draft(tmp_path)
    try:
        review.approve("em-1", send=True, api_key="k", conn=conn,
                       http_request=lambda *a: (500, "boom"),
                       log=lambda *_: None)
        assert False
    except ledger.LedgerError as exc:
        assert "HTTP 500" in str(exc)
    state = conn.execute("SELECT state FROM review_queue").fetchone()[0]
    conn.close()
    assert state == "pending_review"


def test_reject_and_snooze(tmp_path):
    conn = _queued_draft(tmp_path)
    review.snooze("em-1", days=1.0, conn=conn)
    assert review.pending(conn) == []                        # hidden while snoozed
    conn.execute("UPDATE review_queue SET snooze_until=NULL")
    conn.commit()
    review.reject("em-1", reason="not this one", conn=conn)
    state = conn.execute("SELECT state FROM review_queue").fetchone()[0]
    conn.close()
    assert state == "rejected"


# ---------------------------------------------------------------------------
# offer aging bumps + fee-model cap
# ---------------------------------------------------------------------------


def _offer_out(days_ago: float, apn="044", email="jane@x.com", offer=38500):
    import time as _t

    _seed_lead(apn=apn, email=email)
    at = _t.strftime("%Y-%m-%d %H:%M:%S", _t.localtime(_t.time() - days_ago * 86400))
    ledger.set_status("harris_tx", apn, "offer_out", at=at)
    ledger.set_offer_amount("harris_tx", apn, offer)


def test_bump_due_at_seven_days_idempotent_and_restates_offer(tmp_path):
    from . import bumps

    _env_db(tmp_path)
    _offer_out(8.0)
    conn = ledger.connect()
    r1 = bumps.queue_bumps(conn=conn, log=lambda *_: None)
    r2 = bumps.queue_bumps(conn=conn, log=lambda *_: None)
    item = conn.execute(
        "SELECT * FROM review_queue WHERE id LIKE 'bump-%'").fetchone()
    conn.close()
    assert r1["bumps_queued"] == 1 and r2["bumps_queued"] == 0   # idempotent
    assert item["state"] == "pending_review" and item["draft_kind"] == "bump"
    assert "$38,500" in item["draft"]            # the human's recorded offer
    assert "circling back" in item["draft"]


def test_fresh_offer_gets_no_bump_and_backfilled_old_offer_gets_final(tmp_path):
    from . import bumps

    _env_db(tmp_path)
    _offer_out(1.5, apn="044")                    # Thomas at 36h: nothing
    _offer_out(25.0, apn="055", email="old@x.com")
    conn = ledger.connect()
    report = bumps.queue_bumps(conn=conn, log=lambda *_: None)
    items = conn.execute(
        "SELECT id, draft FROM review_queue WHERE id LIKE 'bump-%'").fetchall()
    conn.close()
    assert report["bumps_queued"] == 1
    assert items[0]["id"].endswith("-21d")        # latest tier only, not 7d too
    assert "Last note" in items[0]["draft"]


def test_bump_threads_under_last_reply_and_send_keeps_offer_out(tmp_path):
    from . import bumps

    _env_db(tmp_path)
    _offer_out(8.0)
    conn = ledger.connect()
    ingest_mod.ingest_reply(_reply(text="thinking about it"), conn=conn)
    bumps.queue_bumps(conn=conn, log=lambda *_: None)
    sent = {}
    result = review.approve(
        "bump-harris_tx-044-7d", send=True, api_key="k", conn=conn,
        http_request=lambda m, u, p, k: (sent.update(p), (200, "{}"))[1],
        log=lambda *_: None)
    status = conn.execute(
        "SELECT status FROM leads WHERE apn='044'").fetchone()[0]
    conn.close()
    assert result["sent"]
    assert sent["reply_to_uuid"] == "em-1"        # threads under seller's email
    assert status == "offer_out"                  # bump never moves status


def test_above_cap_lead_is_low_priority_no_holding_promise(tmp_path):
    _env_db(tmp_path)
    ledger.ingest_rows([{
        "APN": "900", "County": "Harris", "State": "TX",
        "Address": "9 Big Rd", "City": "Houston", "Zip": "77028",
        "Email 1": "cliff@x.com", "Lot Size Sqft": "871200",
        "Est. Value": "850,000",
    }], source_list="t")
    conn = ledger.connect()
    ingest_mod.ingest_reply(_reply(id="em-c", email="cliff@x.com",
                                   text="I'd consider 900k"), conn=conn)
    _through_verify(conn, _LLM('{"classification": "price_given", '
                               '"price_mentioned": 900000, "summary": "s"}'))
    report = draft_mod.draft_verified(conn=conn,
                                      llm=_LLM("SHOULD NEVER BE CALLED"),
                                      log=lambda *_: None)
    item = conn.execute("SELECT state, reason, draft FROM review_queue").fetchone()
    conn.close()
    assert report.get("low_priority") == 1
    assert item["state"] == "needs_manual" and item["draft"] is None
    assert "max_asset_value" in item["reason"]


def test_ledger_set_helpers_backdate_stub_and_lookup(tmp_path):
    _env_db(tmp_path)
    # stub creation for a pre-ledger deal
    assert ledger.ensure_lead("waller_tx", "15065", status="offer_out",
                              note="thomas", email="thomas.brown33@icloud.com")
    assert not ledger.ensure_lead("waller_tx", "15065")   # second time: no-op
    hits = ledger.find_leads(email="thomas.brown33@icloud.com")
    assert len(hits) == 1 and hits[0]["status"] == "offer_out"

    _seed_lead(apn="2D", email="deepak@x.com")
    hits = ledger.find_leads(address="1 land rd")
    assert len(hits) == 1 and hits[0]["apn"] == "2D"


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
