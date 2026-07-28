"""tools/browser/cli.py — the browser runner's command line.

    python -m tools.browser.cli seed-login                 # one-time human login
    python -m tools.browser.cli pull --county harris --state tx
    python -m tools.browser.cli --check --county harris --state tx --headful
"""

from __future__ import annotations

import argparse
import sys

try:
    from dotenv import load_dotenv

    load_dotenv(override=True)
except ImportError:
    pass

from .config import load_config


def _build_stamp() -> str:
    """Short commit of the checkout actually running — printed on every
    command so a stale working copy is obvious immediately (a `git pull`
    missed between fixes has silently wasted whole live runs)."""

    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "log", "-1", "--format=%h %cr"],
            capture_output=True, text=True, timeout=5)
        return (out.stdout or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _apply_overrides(config, args):
    changes = {}
    for cli_attr, field in (
        ("lot_min_acres", "lot_min_acres"), ("lot_max_acres", "lot_max_acres"),
        ("assessed_min", "assessed_min_usd"), ("assessed_max", "assessed_max_usd"),
        ("max_export", "max_export_rows"),
    ):
        val = getattr(args, cli_attr, None)
        if val is not None:
            changes[field] = val
    if getattr(args, "no_absentee", False):
        changes["absentee_owner"] = False
    if getattr(args, "tax_delinquent", False):
        changes["tax_delinquent"] = True
    if getattr(args, "no_skiptrace", False):
        changes["run_skiptrace"] = False
    if getattr(args, "headful", False):
        changes["headless"] = False
    return config.mutate(**changes) if changes else config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tools.browser.cli",
        description="PropStream browser runner (login -> filter -> export -> inbox).")
    parser.add_argument("command", nargs="?", default="pull",
                        choices=["pull", "pull-dry", "pull-v2", "seed-login",
                                 "export-list", "export-list-dry",
                                 "skiptrace-list", "skiptrace-list-dry"])
    parser.add_argument("--list-name", dest="list_name", default=None,
                        help="saved list to export (default: today's "
                             "aria-{county}-{state}-{date})")
    parser.add_argument("--check", action="store_true",
                        help="calibration sweep: probe every selector, screenshot each")
    parser.add_argument("--county")
    parser.add_argument("--state", default="tx")
    parser.add_argument("--lot-min-acres", type=float, dest="lot_min_acres")
    parser.add_argument("--lot-max-acres", type=float, dest="lot_max_acres")
    parser.add_argument("--assessed-min", type=float, dest="assessed_min")
    parser.add_argument("--assessed-max", type=float, dest="assessed_max")
    parser.add_argument("--max-export", type=int, dest="max_export")
    parser.add_argument("--years-owned-min", type=int, default=None,
                        help="Years of Ownership minimum (recipe filter)")
    parser.add_argument("--no-absentee", action="store_true")
    parser.add_argument("--tax-delinquent", action="store_true")
    parser.add_argument("--no-skiptrace", action="store_true")
    parser.add_argument("--headful", action="store_true")
    parser.add_argument("--config", default=None, help="YAML overlay (fields + selectors)")
    args = parser.parse_args(argv)

    stamp = _build_stamp()
    if stamp:
        print(f"[cli] build {stamp}", file=sys.stderr)

    config = _apply_overrides(load_config(args.config), args)

    if args.command == "seed-login":
        from .session import seed_login

        seed_login(config)
        return 0

    if args.check:
        return _run_check(config, args)

    if not args.county:
        parser.error("--county is required for a pull")

    if args.command in ("export-list", "export-list-dry",
                        "skiptrace-list", "skiptrace-list-dry"):
        import json
        from datetime import datetime, timezone

        from . import propstream as flow
        from .session import real_driver, resolve_credentials

        user, pw = resolve_credentials(config)
        list_name = args.list_name or config.saved_list_name.format(
            county=args.county, state=args.state,
            date=datetime.now(timezone.utc).strftime("%Y%m%d"))
        skiptrace = args.command.startswith("skiptrace")
        label = "skiptrace-list" if skiptrace else "export-list"
        try:
            with real_driver(config) as driver:
                if skiptrace:
                    report = flow.run_skiptrace_list(
                        driver, config, list_name, username=user, password=pw,
                        dry=args.command.endswith("-dry"))
                else:
                    report = flow.run_export_list(
                        driver, config, list_name, username=user, password=pw,
                        county=args.county, state=args.state,
                        dry=args.command.endswith("-dry"))
        except Exception as exc:  # noqa: BLE001
            print(f"{label} failed: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(report, indent=2))
        return 0

    if args.command in ("pull-dry", "pull-v2"):
        import json

        from . import propstream as flow
        from .session import real_driver, resolve_credentials

        user, pw = resolve_credentials(config)
        # config ranges -> the panel's label-anchored Min/Max fields
        steps = []
        if config.lot_min_acres or config.lot_max_acres:
            steps.append(("Lot Size (SqFt)",
                          int(config.lot_min_acres * 43560)
                          if config.lot_min_acres else None,
                          int(config.lot_max_acres * 43560)
                          if config.lot_max_acres else None))
        if config.assessed_min_usd or config.assessed_max_usd:
            steps.append(("Assessed Total Value",
                          config.assessed_min_usd, config.assessed_max_usd))
        if args.years_owned_min:
            steps.append(("Years of Ownership", args.years_owned_min, None))
        try:
            with real_driver(config) as driver:
                report = flow.run_pull_v2(
                    driver, config, args.county, args.state,
                    username=user, password=pw,
                    recipe_steps=steps,
                    lot_min_sqft=None if steps else 5000,
                    dry=(args.command == "pull-dry"))
        except Exception as exc:  # noqa: BLE001
            print(f"pull-v2 failed: {exc}", file=sys.stderr)
            return 1
        print(json.dumps({k: v for k, v in report.items()}, indent=2))
        if report.get("dry"):
            print("\nDRY RUN — nothing exported. Actions menu above.")
            return 0
        if report.get("downloaded"):
            print(f"\npulled -> {report['downloaded']} (intake will process it)")
            return 0
        return 1

    from .hook import pipeline_replenish

    result = pipeline_replenish(args.county, args.state, config=config)
    if result.get("ok"):
        print(f"pulled -> {result['file']} (intake will process it)")
        return 0
    print(f"pull failed: {result.get('error')}", file=sys.stderr)
    return 2 if result.get("frozen") else 1


def _run_check(config, args) -> int:
    """Calibration sweep — drives the real logged-in page, maps every selector."""

    from .calibrate import calibrate
    from .session import real_driver

    if not args.county:
        args.county = "harris"
    try:
        with real_driver(config, headless=config.headless,
                         selectors_path=args.config) as driver:
            driver.goto(config.app_url)
            # Map whichever state we're in WITHOUT submitting a login form: if
            # the seeded session is live we probe app/filter selectors; if not,
            # land on the login page so login.* selectors resolve. Never POST
            # empty credentials (avoids failed-login noise / lockout).
            if not driver.is_present("app.ready", timeout_ms=4000):
                driver.goto(config.login_url)
                # With REAL stored credentials (never empty ones), attempt the
                # actual login so calibration can reach the authenticated app.
                # A challenge wall stops us cold; an unconfirmed app.ready is
                # fine — we calibrate whatever page we land on.
                creds = None
                try:
                    from .session import resolve_credentials

                    creds = resolve_credentials(config)
                except Exception:  # noqa: BLE001 — no creds => probe-only mode
                    pass
                if creds:
                    from . import propstream as flow
                    from .driver import AuthChallenge, VerificationError

                    try:
                        flow.login(driver, config, *creds)
                        print("login: authenticated with stored credentials")
                    except AuthChallenge as exc:
                        print(f"login: CHALLENGE wall ({exc}) — stopped; "
                              "never bypassed")
                    except VerificationError as exc:
                        print(f"login: submitted, app.ready unconfirmed "
                              f"({exc}) — calibrating the page we're on")
            # clear the single-session dialog + cookie banner if up
            for key in ("session.proceed", "consent.accept"):
                try:
                    if driver.is_present(key, timeout_ms=2500):
                        driver.click(key)
                except Exception:  # noqa: BLE001
                    pass
            summary = getattr(driver, "page_summary", dict)()
            report = calibrate(driver, config)
            report["page"] = summary
            report["dom"] = getattr(driver, "dom_inventory", list)()

            # Stage 2: authenticated? type the county to reveal the search
            # suggestions + whatever filter UI appears — the DOM we still
            # can't see any other way. Read-only: nothing is saved/exported.
            if driver.is_present("app.ready", timeout_ms=4000):
                # The OneTrust overlay intercepts pointer events — clicking
                # Accept once isn't always enough (it can re-render). Retry
                # until the accept button is actually gone.
                for _ in range(3):
                    if not driver.is_present("consent.accept", timeout_ms=1500):
                        break
                    try:
                        driver.click("consent.accept")
                    except Exception:  # noqa: BLE001
                        pass
                try:
                    # the box re-renders on focus (session-generated ids), so
                    # click it and type via raw keyboard, no locator re-use.
                    # Typing alone does NOT run the search — the autocomplete
                    # suggestion must be selected (ArrowDown + Enter).
                    driver.click("search.box")
                    getattr(driver, "type_keys", lambda t: None)(
                        f"{args.county.title()} County, {args.state.upper()}")
                    # capture + click the REAL suggestion (any tag/class);
                    # retry a few beats — the dropdown loads from the network
                    finder = getattr(driver, "find_and_click_suggestion",
                                     lambda n: [])
                    report["suggestions_seen"] = []
                    for _ in range(4):
                        driver.is_present("search.suggestion", timeout_ms=1500)
                        report["suggestions_seen"] = finder(
                            f"{args.county.title()} County")
                        if report["suggestions_seen"]:
                            break
                    driver.screenshot("calib-stage2-suggestions")
                    if not report["suggestions_seen"]:
                        # screenshot-confirmed: the header has an explicit
                        # Search button next to the box — THE execute trigger
                        getattr(driver, "click_text", lambda t: None)("Search")
                    # the header counter chips flip non-zero once geography
                    # is applied — that's the proof the search executed
                    counters = []
                    for _ in range(10):
                        driver.is_present("filters.open", timeout_ms=1500)
                        counters = getattr(driver, "header_counters",
                                           list)()
                        if any(not c.startswith("0 ") for c in counters):
                            break
                    report["search_typed"] = True
                    report["counters_after_search"] = counters
                except Exception as exc:  # noqa: BLE001
                    report["search_typed"] = f"FAILED: {exc}"
                # Open the Filters panel (selectors already calibrated).
                try:
                    if driver.is_present("filters.open", timeout_ms=3000):
                        driver.click("filters.open")
                        driver.is_present("filters.find", timeout_ms=4000)
                        report["filters_opened"] = True
                except Exception as exc:  # noqa: BLE001
                    report["filters_opened"] = f"FAILED: {exc}"
                # Stage 5: the vacant-land dry search — click the Vacant Land
                # classification CHIP (scoped under its heading, not the
                # Lead-List row of the same name), View Properties, and
                # inventory the RESULTS view (count / select-all / export).
                # Read-only: nothing is saved, skip-traced, or exported.
                try:
                    getattr(driver, "click_after_heading",
                            lambda *a: None)("Property Classification(s)",
                                             "Vacant Land")
                    driver.is_present("filters.apply", timeout_ms=1500)
                    getattr(driver, "fill_labeled_range",
                            lambda *a, **k: None)("Lot Size (SqFt)", 5000, None)
                    # the lot-size input opens its own suggestion dropdown
                    # (screenshot-confirmed) — Tab moves focus off and closes
                    # it (Escape closes the whole Filters panel — seen live)
                    getattr(driver, "press_key", lambda k: None)("Tab")
                    driver.screenshot("calib-stage5a-filters-set")
                    # the button's label may carry a live count ('View 28,405
                    # Properties'), so match by contained words, not exact name
                    vp = getattr(driver, "click_button_containing",
                                 lambda w: "")(["View", "Propert"])
                    report["view_properties_btn"] = vp
                    if not vp:
                        driver.click("filters.apply")   # exact-name fallback
                    driver.is_present("results.select_all", timeout_ms=9000) \
                        or driver.is_present("app.ready", timeout_ms=3000)
                    report["results_recon"] = getattr(
                        driver, "results_recon", dict)()
                    driver.screenshot("calib-stage5b-results")
                    # Stage 6: select-all reveals the grid's action bar
                    # (Add to List / Skip Trace / Export). Selecting rows
                    # commits nothing — still read-only.
                    report["checkbox_clicked"] = getattr(
                        driver, "click_first_checkbox", lambda: "")()
                    if report["checkbox_clicked"]:
                        before = set((report["results_recon"] or {})
                                     .get("buttons") or [])
                        driver.is_present("results.add_to_list",
                                          timeout_ms=2500)
                        after = getattr(driver, "visible_button_texts",
                                        list)()
                        report["action_bar"] = [b for b in after
                                                if b not in before]
                        report["all_buttons_after_select"] = after
                        driver.screenshot("calib-stage6-selected")
                except Exception as exc:  # noqa: BLE001
                    report["results_recon"] = {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        print(f"calibration could not launch a browser: {exc}", file=sys.stderr)
        return 1
    import json
    import os

    # Full report -> file on the VPS (keeps the pasted-back output small).
    art = os.path.expanduser(config.artifact_dir)
    os.makedirs(art, exist_ok=True)
    report_path = os.path.join(art, "calib_report.json")
    try:
        with open(report_path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
    except Exception:  # noqa: BLE001
        report_path = "(could not write)"

    # Compact stdout: progress flags + the RESULTS recon (the only new data).
    print("=== calibration summary ===")
    print(f"login          : {report.get('login', 'reused session')}")
    print(f"page           : {(report.get('page') or {}).get('url')}")
    print(f"search typed   : {report.get('search_typed')}")
    print(f"suggestions    : {report.get('suggestions_seen')}")
    print(f"counters       : {report.get('counters_after_search')}")
    print(f"filters opened : {report.get('filters_opened')}")
    print(f"view-props btn : {report.get('view_properties_btn')!r}")
    rr = report.get("results_recon") or {}
    if rr.get("error"):
        print(f"results recon  : ERROR {rr['error']}")
    else:
        print(f"results count  : {rr.get('count_text', '')!r}")
        print(f"results buttons: {rr.get('buttons')}")
        print(f"checkbox-like  : {len(rr.get('checkbox_like') or [])} found")
    print(f"checkbox click : {report.get('checkbox_clicked')!r}")
    print(f"ACTION BAR     : {report.get('action_bar')}")
    print(f"all btns after : {report.get('all_buttons_after_select')}")
    print(f"\nfull report    : {report_path}")
    print("screenshots    :", config.artifact_dir)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
