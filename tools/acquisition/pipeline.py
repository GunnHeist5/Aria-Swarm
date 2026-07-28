"""tools/acquisition/pipeline.py — one county, end to end, unattended.

    pull -> save list -> skip trace -> WAIT for contacts -> export -> ledger

Everything the operator did by hand on 2026-07-28, in one call, with the
same fail-closed gates. What it deliberately does NOT do:

  * enroll — pushing outreach stays a human decision (the standing rule);
    the run ends with a batch sitting in the ledger awaiting `enroll --push`.
  * retry a skip-trace order — a second order can double-spend credits, so
    a trace that never lands is reported, never re-placed.

The trace wait is a POLL, not a sleep: skip trace usually completes in
minutes (694 records < 10 min, 4,475 records < 1 h observed), but the
export is only worth taking once the contact columns are actually filled,
so the flow re-exports until the fill-rate clears a floor or the attempts
run out. Every attempt's file is kept — a partial export is still leads.

Seams (`flow=`, `ingest=`, `driver_factory=`) keep the whole thing testable
with zero browser, matching the repo's convention.
"""

from __future__ import annotations

from pathlib import Path

from . import ledger

# fill-rate floor: below this the trace probably has not landed yet
MIN_EMAIL_FILL = 0.25
# how many times to re-export while waiting for the trace to land
TRACE_POLL_ATTEMPTS = 8
TRACE_POLL_SECONDS = 240


def email_fill_rate(path: str | Path) -> tuple[int, float]:
    """(rows, fraction carrying an email) for an export on disk."""

    from ..integrations.leadfile import parse_propstream

    rows = parse_propstream(path)
    if not rows:
        return 0, 0.0
    filled = sum(1 for r in rows
                 if str(r.get("Email 1") or "").strip()
                 or str(r.get("Email 2") or "").strip())
    return len(rows), filled / len(rows)


def run_county(county: str, state: str, *, config, username: str, password: str,
               recipe_steps=(), skiptrace: bool = True,
               list_name: str | None = None,
               min_email_fill: float = MIN_EMAIL_FILL,
               poll_attempts: int = TRACE_POLL_ATTEMPTS,
               poll_seconds: int = TRACE_POLL_SECONDS,
               driver_factory=None, flow=None, ingest=None, sleep=None,
               log=print) -> dict:
    """Pull a county, trace it, export it, load it into the ledger.

    ``list_name`` skips the pull and works an EXISTING PropStream list —
    the way to enrich lists built before this pipeline existed (trace them,
    wait for the contacts, export, backfill the ledger).

    Returns a report; raises only when a stage fails in a way that makes
    the next stage meaningless (the browser flows fail closed themselves).
    """

    if flow is None:  # pragma: no cover - real wiring
        from ..browser import propstream as flow
    if driver_factory is None:  # pragma: no cover - real wiring
        from ..browser.session import real_driver

        driver_factory = real_driver
    if ingest is None:
        ingest = ledger.ingest_rows
    if sleep is None:  # pragma: no cover - real wiring
        import time

        sleep = time.sleep

    report: dict = {"county": county, "state": state, "stages": [],
                    "list_name": None, "exports": [], "rows": 0,
                    "email_fill": 0.0, "ingested": None, "traced": False}

    def stage(name: str, **extra) -> None:
        report["stages"].append({"stage": name, **extra})
        log(f"[pipeline] {name}" + (f" {extra}" if extra else ""))

    # ---- 1. pull + save the list (or adopt an existing one) ---------------
    if list_name:
        report["list_name"] = list_name
        stage("existing_list", list_name=list_name)
    else:
        # The pull NEVER skip traces: the grid's own trace path is
        # uncalibrated, and tracing belongs to run_skiptrace_list against
        # the saved list.
        pull_config = config.mutate(run_skiptrace=False) \
            if hasattr(config, "mutate") else config
        with driver_factory(pull_config) as driver:
            pull = flow.run_pull_v2(driver, pull_config, county, state,
                                    username=username, password=password,
                                    recipe_steps=recipe_steps,
                                    lot_min_sqft=None if recipe_steps else 5000,
                                    dry=False, log=log)
        report["list_name"] = list_name = pull.get("list_name")
        report["count"] = pull.get("count")
        stage("pulled", count=pull.get("count"), list_name=list_name)
        if not list_name:
            raise RuntimeError("pull did not report a saved list name")

    # ---- 2. skip trace (never re-ordered on a later pass) -----------------
    if skiptrace:
        with driver_factory(config) as driver:
            trace = flow.run_skiptrace_list(driver, config, list_name,
                                            username=username,
                                            password=password, dry=False,
                                            log=log)
        report["traced"] = bool(trace.get("started"))
        report["order_summary"] = trace.get("order_summary")
        stage("skiptrace_ordered", submitted=report["traced"])
        if not report["traced"]:
            log("[pipeline] WARNING: the skip-trace order did not confirm; "
                "continuing to export WITHOUT re-ordering (double-spend "
                "risk). Contacts may be missing.")

    # ---- 3. export, polling until the traced contacts show up ------------
    exported = ""
    rows = fill = 0
    for attempt in range(1, poll_attempts + 1):
        with driver_factory(config) as driver:
            ex = flow.run_export_list(driver, config, list_name,
                                      username=username, password=password,
                                      county=county, state=state, dry=False,
                                      log=log)
        exported = ex.get("downloaded") or ""
        if not exported:
            raise RuntimeError("export produced no file")
        report["exports"].append(exported)
        rows, fill = email_fill_rate(exported)
        report["rows"], report["email_fill"] = rows, fill
        stage("exported", attempt=attempt, rows=rows,
              email_fill=round(fill, 3), path=exported)
        if not skiptrace or fill >= min_email_fill:
            break
        if attempt < poll_attempts:
            log(f"[pipeline] email fill {fill:.0%} < {min_email_fill:.0%} — "
                f"the trace is still landing; re-exporting in "
                f"{poll_seconds}s ({attempt}/{poll_attempts})")
            sleep(poll_seconds)
    else:
        log(f"[pipeline] WARNING: email fill stalled at {fill:.0%} after "
            f"{poll_attempts} exports — ingesting what we have")

    # ---- 4. ledger ---------------------------------------------------------
    from ..integrations.leadfile import parse_propstream

    report["ingested"] = ingest(parse_propstream(exported),
                                source_list=Path(exported).name)
    stage("ingested", **{k: v for k, v in report["ingested"].items()
                         if k != "source_list"})
    log(f"[pipeline] {county}/{state} done — {rows} rows, "
        f"{fill:.0%} with email, "
        f"{report['ingested'].get('inserted', 0)} new, "
        f"{report['ingested'].get('contacts_backfilled', 0)} backfilled. "
        "Enrollment is NOT automatic: review with "
        "`acquire enroll --include-unscreened` then add --push.")
    return report
