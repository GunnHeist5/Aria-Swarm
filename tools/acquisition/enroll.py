"""tools/acquisition/enroll.py — ledger -> Instantly campaign enrollment.

The ONLY sanctioned path from the ledger into outreach. Eligibility is
decided by ledger state (status + verdict + suppression), the suppression
check runs again as the LAST gate before every batch, and pushes go through
the existing ``tools.integrations.instantly`` adapter (dry-run default,
``skip_if_in_campaign`` idempotency, masked reporting).

Every enrolled lead carries the mandatory custom fields — apn, county,
source_list, verdict, mao, open_at, walk_at — plus the campaign template
variables the running sequence already uses ({{propertyCity}} etc.), so one
payload serves both the email copy and the reply agent's later ledger match.
"""

from __future__ import annotations

import sqlite3
import time

from ..integrations import instantly
from ..integrations.leadfile import SQFT_PER_ACRE
from . import ledger
from .config import AcquisitionConfig, DEFAULT_CONFIG


def eligible_leads(config: AcquisitionConfig = DEFAULT_CONFIG, *,
                   include_unscreened: bool = False,
                   conn: sqlite3.Connection | None = None) -> list[sqlite3.Row]:
    """Leads the ledger allows into a campaign, one row per email (first wins).

    Default gate follows the brief exactly: only ``SEND CONTRACT``/
    ``NEGOTIATE`` verdicts. ``include_unscreened`` additionally admits
    verdict-less ``new`` leads — the pre-enrichment workflow — and is an
    explicit, logged operator choice, never a default.
    """

    own = conn is None
    conn = conn or ledger.connect()
    try:
        ledger.init_db(conn)
        placeholders = ",".join("?" for _ in config.enrollable_statuses)
        vplaceholders = ",".join("?" for _ in config.eligible_verdicts)
        verdict_clause = f"verdict IN ({vplaceholders})"
        params: list = list(config.enrollable_statuses) + list(config.eligible_verdicts)
        if include_unscreened:
            verdict_clause = f"({verdict_clause} OR verdict IS NULL)"
        rows = conn.execute(
            f"SELECT * FROM leads WHERE status IN ({placeholders}) "
            f"AND email IS NOT NULL AND {verdict_clause} "
            "ORDER BY county_key, apn", params).fetchall()
    finally:
        if own:
            conn.close()

    seen: set[str] = set()
    out = []
    for row in rows:
        email = (row["email"] or "").lower()
        if email in seen:
            continue
        seen.add(email)
        out.append(row)
    return out


def build_payload(row: sqlite3.Row) -> dict:
    """One ledger row -> the Instantly lead payload (adapter shape)."""

    lot_acres = None
    if row["lot_sqft"]:
        lot_acres = round(row["lot_sqft"] / SQFT_PER_ACRE, 2)
    template_vars = {
        "propertyAddress": row["address"],
        "propertyCity": row["city"],
        "propertyZip": row["zip"],
        "state": row["state"],
        "lotAcres": lot_acres,
        "estValue": row["est_value"],
    }
    # Mandatory on EVERY lead (brief) — present even when empty, so the reply
    # agent can rely on the keys existing.
    mandatory = {
        "apn": row["apn"] or "",
        "county": row["county_name"] or row["county_key"] or "",
        "source_list": row["source_list"] or "",
        "verdict": row["verdict"] or "",
        "mao": row["mao"] if row["mao"] is not None else "",
        "open_at": row["open_at"] if row["open_at"] is not None else "",
        "walk_at": row["walk_at"] if row["walk_at"] is not None else "",
    }
    custom = {k: v for k, v in template_vars.items() if v not in (None, "")}
    custom.update(mandatory)
    return {
        "email": row["email"],
        "first_name": row["first_name"] or "",
        "last_name": row["last_name"] or "",
        "custom_variables": custom,
    }


def _campaign_for(row: sqlite3.Row, override: str | None,
                  conn: sqlite3.Connection) -> str | None:
    if override:
        return override
    hit = conn.execute(
        "SELECT campaign_id FROM campaign_map WHERE county_key=?",
        (row["county_key"],)).fetchone()
    if hit:
        return hit["campaign_id"]
    from ..integrations.lead_intake import resolve_campaign

    return resolve_campaign(row["county_key"])


def enroll(config: AcquisitionConfig = DEFAULT_CONFIG, *,
           push: bool = False, limit: int = 100,
           campaign_id: str | None = None,
           include_unscreened: bool = False,
           api_key: str = "",
           http_post=None, sleep=time.sleep,
           log=print) -> dict:
    """Enroll eligible leads. Dry-run unless ``push``. Returns a report."""

    conn = ledger.connect()
    try:
        candidates = eligible_leads(config, include_unscreened=include_unscreened,
                                    conn=conn)
        suppressed = ledger.suppressed_set("email", conn=conn)

        report = {"eligible": len(candidates), "suppressed_last_gate": 0,
                  "no_campaign": 0, "dry_run": not push,
                  "include_unscreened": include_unscreened,
                  "campaigns": {}, "enrolled": 0, "errors": 0}
        if include_unscreened:
            log("[enroll] include_unscreened=TRUE — verdict gate bypassed by "
                "operator for this run")

        # LAST GATE: suppression + hard-status assertion, per lead, at send time.
        by_campaign: dict[str, list[sqlite3.Row]] = {}
        for row in candidates:
            if (row["email"] or "").lower() in suppressed:
                report["suppressed_last_gate"] += 1
                continue
            ledger.assert_enrollable(row, suppressed)
            campaign = _campaign_for(row, campaign_id, conn)
            if not campaign:
                report["no_campaign"] += 1
                continue
            by_campaign.setdefault(campaign, []).append(row)

        remaining = limit
        for campaign, rows in by_campaign.items():
            rows = rows[:remaining]
            if not rows:
                break
            remaining -= len(rows)
            payloads = [build_payload(r) for r in rows]
            by_email = {p["email"].lower(): r for p, r in zip(payloads, rows)}

            def _on_result(lead: dict, outcome: str) -> None:
                row = by_email.get(lead["email"].lower())
                if row is not None and outcome in ("pushed", "skipped_existing"):
                    ledger.set_status(row["county_key"], row["apn"], "enrolled",
                                      note=f"instantly:{campaign}:{outcome}",
                                      conn=conn)

            kwargs = {"api_key": api_key, "campaign_id": campaign,
                      "limit": len(payloads), "dry_run": not push,
                      "sleep": sleep, "on_result": _on_result}
            if http_post is not None:
                kwargs["http_post"] = http_post
            sub = instantly.push_leads(payloads, **kwargs)
            report["campaigns"][campaign] = sub
            report["enrolled"] += sub["pushed"] + sub["skipped_existing"]
            report["errors"] += sub["errors"]
            if sub.get("aborted"):
                report["aborted"] = sub["aborted"]
                break
        return report
    finally:
        conn.close()
