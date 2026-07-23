"""Offline tests for M4 — county scoring + enrichment bridge, seams stubbed."""

from __future__ import annotations

import os
from pathlib import Path

from . import counties, enrich, ledger
from .config import DEFAULT_CONFIG


def _env(tmp_path: Path) -> None:
    os.environ["ACQUISITION_DB"] = str(tmp_path / "acq.db")
    os.environ["COUNTIES_DB"] = str(tmp_path / "counties.db")
    os.environ["SUPPRESSION_STORE"] = str(tmp_path / "sup.json")


def _seed(apn="044", email="j@x.com", county="Harris", state="TX"):
    ledger.ingest_rows([{
        "APN": apn, "County": county, "State": state, "Address": "1 Land Rd",
        "City": "Houston", "Zip": "77028", "Email 1": email,
        "Lot Size Sqft": "21780", "Est. Value": "40000",
    }], source_list="t")


# ---------------------------------------------------------------------------
# county rubric
# ---------------------------------------------------------------------------


def test_rubric_full_marks_and_ranking(tmp_path):
    _env(tmp_path)
    good = {"county_key": "brazoria_tx", "growth_pct": 6.0,
            "median_lot_value": 60_000, "data_cad": 1, "data_gis": 1,
            "data_tax": 1, "dispo_listings": 400}
    result = counties.score_county(good)
    assert result["score"] == 100.0 and not result["gaps"]

    counties.upsert("brazoria_tx", **{k: v for k, v in good.items()
                                      if k != "county_key"})
    counties.upsert("nowhere_ks", growth_pct=0.2, median_lot_value=500_000)
    cards = counties.scorecard()
    assert [c["county_key"] for c in cards] == ["brazoria_tx", "nowhere_ks"]


def test_guru_counties_get_saturation_penalty():
    base = {"growth_pct": 6.0, "median_lot_value": 60_000, "data_cad": 1,
            "data_gis": 1, "data_tax": 1, "dispo_listings": 400}
    putnam = counties.score_county({**base, "county_key": "putnam_fl"})
    fresh = counties.score_county({**base, "county_key": "brazoria_tx"})
    assert putnam["saturated"] and putnam["score"] == fresh["score"] - 15.0
    for key in ("marion_fl", "mohave_az", "costilla_co", "hudspeth_tx"):
        assert key in counties.GURU_COUNTIES


def test_rubric_missing_inputs_reported_as_gaps_not_guessed():
    result = counties.score_county({"county_key": "mystery_ok"})
    assert result["score"] == 0.0
    assert set(result["gaps"]) == {"growth_pct", "median_lot_value",
                                   "data availability", "dispo_listings"}


def test_band_fit_edges():
    def fit(v):
        return counties.score_county(
            {"county_key": "x_tx", "median_lot_value": v})["parts"]["band_fit"]
    assert fit(20_000) == 20.0 and fit(150_000) == 20.0
    assert fit(12_000) == 10.0 and fit(200_000) == 10.0
    assert fit(5_000) == 0.0 and fit(400_000) == 0.0


def test_research_seam_extracts_conservatively_and_fails_closed():
    hits = [{"title": "Census", "description": "the county grew 4.2% since 2020"}]
    fields = counties.research_county("Brazoria", "TX",
                                      search=lambda q: hits)
    assert fields["growth_pct"] == 4.2 and "Census" in fields["notes"]

    fields = counties.research_county(
        "Brazoria", "TX", search=lambda q: [{"title": "spam",
                                             "description": "up 900% growth!!"}])
    assert fields["growth_pct"] is None            # absurd number rejected

    fields = counties.research_county(
        "Brazoria", "TX",
        search=lambda q: (_ for _ in ()).throw(RuntimeError("api down")))
    assert fields["growth_pct"] is None and "failed" in fields["notes"]


# ---------------------------------------------------------------------------
# enrichment bridge
# ---------------------------------------------------------------------------


def test_screener_results_write_back_and_advance_status(tmp_path):
    _env(tmp_path)
    _seed(apn="044")
    _seed(apn="055")

    def fake_screener(rows, adapter):
        assert adapter == "harris"
        out = []
        for r in rows:
            r = dict(r)
            if r["APN"] == "044":
                r.update(verdict="NEGOTIATE", mao=20000, open_at=17000,
                         walk_at=25000, retail_estimate=40000,
                         comp_evidence="3 comps 77028", frontage=120,
                         flood_zone="X", shape_flag="")
            else:
                r.update(verdict="PASS", shape_flag="SLIVER")
            out.append(r)
        return out

    report = enrich.enrich_county("harris_tx", run_screener=fake_screener,
                                  log=lambda *_: None)
    assert report["screened"] == 1 and report["killed"] == 1
    conn = ledger.connect()
    rows = {r["apn"]: r for r in conn.execute("SELECT * FROM leads")}
    conn.close()
    assert rows["044"]["status"] == "screened"
    assert rows["044"]["retail_estimate"] == 40000
    assert rows["044"]["enrichment_source"] == "screener"
    assert "77028" in rows["044"]["evidence"]
    assert rows["055"]["status"] == "killed"


def test_needs_manual_screener_rows_stay_new(tmp_path):
    _env(tmp_path)
    _seed(apn="044")

    def fake_screener(rows, adapter):
        return [dict(rows[0], needs_manual_reason="roads unavailable")]

    report = enrich.enrich_county("harris_tx", run_screener=fake_screener,
                                  log=lambda *_: None)
    assert report["needs_manual"] == 1
    conn = ledger.connect()
    assert conn.execute("SELECT status FROM leads").fetchone()[0] == "new"
    conn.close()


def test_no_adapter_county_routes_to_browsing(tmp_path):
    _env(tmp_path)
    _seed(apn="9-1", county="Brazoria", state="TX")
    report = enrich.enrich_county("brazoria_tx", run_screener=None,
                                  log=lambda *_: None)
    assert report["browsing_needed"] == 1

    tasks = enrich.browsing_tasks("brazoria_tx")
    assert len(tasks) == 1
    assert "9-1" in tasks[0] and "UNTRUSTED" in tasks[0]
    assert "msc.fema.gov" in tasks[0]


def test_apply_browsing_result_flags_reduced_confidence(tmp_path):
    _env(tmp_path)
    _seed(apn="9-1", county="Brazoria", state="TX")
    verdict = enrich.apply_browsing_result(
        "brazoria_tx", "9-1",
        {"retail_estimate": 40000, "flood_zone": "X", "frontage": "80",
         "shape_flag": "OK", "kill": False, "comps": ["a"]})
    assert verdict == "NEGOTIATE"
    conn = ledger.connect()
    row = conn.execute("SELECT * FROM leads").fetchone()
    conn.close()
    assert row["enrichment_source"] == "browsing"
    assert row["mao"] == 20000.0 and row["status"] == "screened"

    # and the reply verifier downgrades confidence for browsing sources
    from .reply.verify import verify_lead

    conn = ledger.connect()
    v = verify_lead("brazoria_tx", "9-1", conn=conn)
    conn.close()
    assert v["verified"] and "browsing" in v["confidence"]


def test_apply_browsing_kill_or_garbage_is_pass_never_guessed(tmp_path):
    _env(tmp_path)
    _seed(apn="9-1", county="Brazoria", state="TX")
    assert enrich.apply_browsing_result(
        "brazoria_tx", "9-1", {"retail_estimate": "not a number"}) == "PASS"


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
