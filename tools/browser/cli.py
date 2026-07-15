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
            from . import propstream

            driver.goto(config.app_url)
            # Best-effort: get to the filter view so those selectors can resolve.
            try:
                propstream.login(driver, config, "", "")
            except Exception:  # noqa: BLE001 — check maps state, doesn't require login success
                pass
            report = calibrate(driver, config)
    except Exception as exc:  # noqa: BLE001
        print(f"calibration could not launch a browser: {exc}", file=sys.stderr)
        return 1
    import json

    print(render_report(report))
    print("screenshots under:", config.artifact_dir)
    print(json.dumps(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
