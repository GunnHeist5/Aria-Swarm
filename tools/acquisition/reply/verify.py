"""tools/acquisition/reply/verify.py — the verification routine, automated.

Rule 0 of the playbook, in code: no number goes in a draft unless THIS
routine says the lead is verified — fresh county-record enrichment with a
retail estimate — and computes the offer box from it. Stale, missing, or
browsing-sourced enrichment can trigger a re-run (injectable ``rerun`` seam,
wired to the screener on the VPS); if verification still isn't clean, the
answer is ``verified: False`` and the drafter is forced onto the holding
template.

Offer box (pure, table-tested)::

    buyer_ceiling = retail * ceiling_ratio
    mao           = buyer_ceiling - target_fee
    open_at       = mao * open_at_ratio
    walk_at       = buyer_ceiling - min_fee
"""

from __future__ import annotations

import json
import sqlite3
import time

from .. import ledger
from ..config import AcquisitionConfig, DEFAULT_CONFIG

# Enrichment older than this is stale — county data and comps move.
VERIFY_MAX_AGE_DAYS = 45


def offer_box(retail: float, config: AcquisitionConfig = DEFAULT_CONFIG) -> dict:
    ceiling = retail * config.buyer_ceiling_ratio
    mao = ceiling - config.target_fee_usd
    return {
        "retail": round(retail, 2),
        "buyer_ceiling": round(ceiling, 2),
        "mao": round(mao, 2),
        "open_at": round(mao * config.open_at_ratio, 2),
        "walk_at": round(ceiling - config.min_fee_usd, 2),
    }


def _age_days(stamp: str | None) -> float | None:
    if not stamp:
        return None
    try:
        then = time.mktime(time.strptime(stamp, "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        return None
    return (time.time() - then) / 86400.0


def verify_lead(county: str, apn: str, *,
                config: AcquisitionConfig = DEFAULT_CONFIG,
                conn: sqlite3.Connection, rerun=None, log=print) -> dict:
    """-> {"verified", "confidence", "reason", "offer_box", "evidence"}.

    ``rerun(county, apn)`` (optional seam) re-enriches via the screener and
    writes back to the ledger; it is attempted at most once, and any failure
    degrades to unverified — never to a guessed number.
    """

    def _load() -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM leads WHERE county_key=? AND apn=?",
            (county, apn)).fetchone()

    row = _load()
    if row is None:
        return {"verified": False, "reason": f"no such lead {county}/{apn}",
                "confidence": None, "offer_box": None, "evidence": None}

    def _assess(r: sqlite3.Row) -> tuple[bool, str]:
        if not r["verdict"] or r["verdict"] == "PASS":
            return False, "no usable enrichment verdict"
        if r["retail_estimate"] is None:
            return False, "no retail estimate on record"
        age = _age_days(r["screened_at"])
        if age is None or age > VERIFY_MAX_AGE_DAYS:
            return False, f"enrichment stale ({age and round(age)} days)"
        return True, "fresh"

    ok, why = _assess(row)
    if not ok and rerun is not None:
        log(f"[verify] {county}/{apn}: {why} -> re-running enrichment")
        try:
            rerun(county, apn)
            row = _load()
            ok, why = _assess(row)
        except Exception as exc:  # noqa: BLE001 — fail to "unverified", never guess
            why = f"re-enrichment failed: {exc}"
            ok = False

    if not ok:
        return {"verified": False, "reason": why, "confidence": None,
                "offer_box": None, "evidence": None}

    confidence = ("standard" if (row["enrichment_source"] or "") == "screener"
                  else "reduced (browsing-sourced)")
    evidence = None
    if row["evidence"]:
        try:
            evidence = json.loads(row["evidence"])
        except ValueError:
            evidence = {"raw": row["evidence"]}
    return {
        "verified": True,
        "confidence": confidence,
        "reason": "fresh county-record enrichment",
        "offer_box": offer_box(float(row["retail_estimate"]), config),
        "evidence": evidence or {},
        "verdict": row["verdict"],
        "screened_at": row["screened_at"],
    }


def verify_classified(*, config: AcquisitionConfig = DEFAULT_CONFIG,
                      conn: sqlite3.Connection, rerun=None, log=print) -> dict:
    """Run verification for every ``classified`` queue item with a lead."""

    ledger.init_db(conn)
    items = conn.execute(
        "SELECT * FROM review_queue WHERE state='classified' "
        "AND county_key IS NOT NULL ORDER BY ts").fetchall()
    report = {"verified": 0, "unverified": 0}
    for item in items:
        result = verify_lead(item["county_key"], item["apn"], config=config,
                             conn=conn, rerun=rerun, log=log)
        report["verified" if result["verified"] else "unverified"] += 1
        conn.execute(
            "UPDATE review_queue SET verification=?, state='verified', "
            "updated_at=? WHERE id=?",
            (json.dumps(result), ledger._stamp(), item["id"]))
        conn.commit()
    return report
