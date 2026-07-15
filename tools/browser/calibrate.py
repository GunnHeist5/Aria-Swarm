"""tools/browser/calibrate.py — the --check convention for the browser runner.

The dev sandbox can't reach login-walled PropStream, so SELECTORS defaults are
guesses. This drives the REAL logged-in flow on the VPS but, unlike a live
pull, continues PAST a missing element to map every key in one pass — printing
FOUND/MISSING per selector plus a screenshot for each, so the operator fixes a
drifted selector in browser.yaml (data, not code) and re-runs until green.

Also validates the exported CSV's header contract against the existing parser,
so a PropStream layout change surfaces before real leads flow.
"""

from __future__ import annotations

import json

from .config import SELECTORS, BrowserConfig, DEFAULT_CONFIG, config_hash
from .driver import PageDriver


def calibrate(driver: PageDriver, config: BrowserConfig = DEFAULT_CONFIG,
              *, keys=None) -> dict:
    """Probe each logical key for presence; screenshot each. Returns a report."""

    probe_keys = list(keys or SELECTORS.keys())
    found, missing = [], []
    for key in probe_keys:
        try:
            present = driver.is_present(key, timeout_ms=2500)
        except Exception:  # noqa: BLE001 — a bad spec must not abort the sweep
            present = False
        (found if present else missing).append(key)
        try:
            driver.screenshot(f"calib-{key.replace('.', '_')}")
        except Exception:  # noqa: BLE001
            pass
    return {
        "config_hash": config_hash(config),
        "found": found,
        "missing": missing,
        "ok": not missing,
    }


def validate_headers(csv_path: str) -> dict:
    """Confirm a PropStream export parses and carries the columns the pipeline
    reads (address/APN + at least one email column + a lot-size signal)."""

    from tools.integrations.leadfile import EMAIL_COLUMNS, parse_propstream

    rows = parse_propstream(csv_path)
    if not rows:
        return {"ok": False, "detail": "export parsed to zero rows"}
    headers = set(rows[0].keys())
    needed = {"Address", "APN"}
    has_email_col = bool(headers & set(EMAIL_COLUMNS))
    has_lot = "Lot Size Sqft" in headers or "Lot Size Acres" in headers
    missing = sorted(needed - headers)
    return {
        "ok": not missing and has_email_col and has_lot,
        "rows": len(rows),
        "missing_core": missing,
        "has_email_column": has_email_col,
        "has_lot_size": has_lot,
    }


def render_report(report: dict, headers: dict | None = None) -> str:
    lines = [f"SELECTOR CALIBRATION  (config_hash {report['config_hash']})"]
    for key in report["found"]:
        lines.append(f"[FOUND  ] {key}")
    for key in report["missing"]:
        lines.append(f"[MISSING] {key}")
    if headers is not None:
        lines.append(
            "HEADER CONTRACT: "
            + ("ok" if headers.get("ok") else f"PROBLEM {json.dumps(headers)}"))
    lines.append("RESULT: " + ("OK" if report["ok"]
                 else "FIX " + ", ".join(report["missing"])))
    return "\n".join(lines)
