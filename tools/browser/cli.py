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
                        choices=["pull", "seed-login"])
    parser.add_argument("--check", action="store_true",
                        help="calibration sweep: probe every selector, screenshot each")
    parser.add_argument("--county")
    parser.add_argument("--state", default="tx")
    parser.add_argument("--lot-min-acres", type=float, dest="lot_min_acres")
    parser.add_argument("--lot-max-acres", type=float, dest="lot_max_acres")
    parser.add_argument("--assessed-min", type=float, dest="assessed_min")
    parser.add_argument("--assessed-max", type=float, dest="assessed_max")
    parser.add_argument("--max-export", type=int, dest="max_export")
    parser.add_argument("--no-absentee", action="store_true")
    parser.add_argument("--tax-delinquent", action="store_true")
    parser.add_argument("--no-skiptrace", action="store_true")
    parser.add_argument("--headful", action="store_true")
    parser.add_argument("--config", default=None, help="YAML overlay (fields + selectors)")
    args = parser.parse_args(argv)

    config = _apply_overrides(load_config(args.config), args)

    if args.command == "seed-login":
        from .session import seed_login

        seed_login(config)
        return 0

    if args.check:
        return _run_check(config, args)

    if not args.county:
        parser.error("--county is required for a pull")

    from .hook import pipeline_replenish

    result = pipeline_replenish(args.county, args.state, config=config)
    if result.get("ok"):
        print(f"pulled -> {result['file']} (intake will process it)")
        return 0
    print(f"pull failed: {result.get('error')}", file=sys.stderr)
    return 2 if result.get("frozen") else 1


def _run_check(config, args) -> int:
    """Calibration sweep — drives the real logged-in page, maps every selector."""

    from .calibrate import calibrate, render_report
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
                fill = getattr(driver, "force_fill", driver.fill)
                try:
                    driver.click("search.box")
                    fill("search.box", f"{args.county.title()} County, "
                                       f"{args.state.upper()}")
                    driver.is_present("search.suggestion", timeout_ms=5000)
                    report["dom_after_search"] = \
                        getattr(driver, "dom_inventory", list)(60)
                    driver.screenshot("calib-stage2-search-typed")
                except Exception as exc:  # noqa: BLE001
                    report["dom_after_search"] = [{"error": str(exc)}]
                # Stage 3: open the Filters panel and inventory it — the
                # whole filter selector set lives in there.
                try:
                    if driver.is_present("filters.open", timeout_ms=3000):
                        driver.click("filters.open")
                        driver.is_present("filters.find", timeout_ms=4000)
                        report["dom_filters"] = \
                            getattr(driver, "dom_inventory", list)(90)
                        driver.screenshot("calib-stage3-filters-open")
                except Exception as exc:  # noqa: BLE001
                    report["dom_filters"] = [{"error": str(exc)}]
                # Stage 4: probe Find-a-Filter with each term the recipes
                # need — reveals every filter section's real name + option
                # chips. Read-only; the find box is cleared between probes.
                if driver.is_present("filters.find", timeout_ms=2000):
                    report["filter_probes"] = {}
                    for term in ("Property Type", "Property Class",
                                 "Lot Size", "Ownership", "Owner Occupied",
                                 "Improvement", "Equity", "Tax"):
                        try:
                            fill("filters.find", term)
                            driver.is_present("filters.apply", timeout_ms=1500)
                            report["filter_probes"][term] = \
                                getattr(driver, "dom_inventory", list)(45)
                            driver.screenshot(
                                "calib-stage4-" + term.lower().replace(" ", "-"))
                        except Exception as exc:  # noqa: BLE001
                            report["filter_probes"][term] = [{"error": str(exc)}]
                    try:
                        fill("filters.find", "")
                    except Exception:  # noqa: BLE001
                        pass
    except Exception as exc:  # noqa: BLE001
        print(f"calibration could not launch a browser: {exc}", file=sys.stderr)
        return 1
    import json

    if report.get("page"):
        print(f"PAGE url   : {report['page'].get('url')}")
        print(f"PAGE title : {report['page'].get('title')}")
        print(f"PAGE text  : {report['page'].get('text', '')[:300]}")
    if report.get("dom"):
        print("DOM INVENTORY (visible elements — selector raw material):")
        for el in report["dom"]:
            attrs = " ".join(f"{k}={v!r}" for k, v in el.items()
                             if v and k != "tag")
            print(f"  <{el['tag']}> {attrs}")
    def _print_els(els):
        for el in els:
            attrs = " ".join(f"{k}={v!r}" for k, v in el.items()
                             if v and k != "tag")
            print(f"  <{el.get('tag', '?')}> {attrs}")

    for section, label in (("dom_after_search",
                            "DOM AFTER TYPING COUNTY (suggestions):"),
                           ("dom_filters",
                            "DOM WITH FILTERS PANEL OPEN:")):
        if report.get(section):
            print(label)
            _print_els(report[section])
    for term, els in (report.get("filter_probes") or {}).items():
        # only the informative elements: texts and labeled inputs, skipping
        # the constant sidebar/map chrome
        print(f"FILTER PROBE {term!r}:")
        _print_els([e for e in els
                    if e.get("text") or e.get("placeholder") or e.get("error")])
    print(render_report(report))
    print("screenshots under:", config.artifact_dir)
    print(json.dumps(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
