"""tools/browser/propstream.py — the PropStream pull flow.

Speaks ONLY logical keys against the PageDriver seam — no Playwright, no raw
selectors — so this whole file runs against FakePageDriver in tests with zero
browser. The sequence:

    login -> apply vacant-land filters -> save list -> skip-trace -> export CSV
          -> move the file into the leads inbox

and stops there. The existing lead_intake pipeline owns dedupe/suppression/
market-routing/Instantly from the inbox onward.

Every gate FAILS CLOSED: a login/filter/skiptrace step that can't confirm its
verify anchor raises rather than pressing on to export blank or wrong data. A
challenge wall raises AuthChallenge for the caller to route to HITL freeze.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

from .config import CHALLENGE_KEYS, VERIFY_GATES, BrowserConfig, DEFAULT_CONFIG
from .driver import AuthChallenge, PageDriver, VerificationError
from .retry import step


def _detect_challenge(driver: PageDriver) -> None:
    """Raise AuthChallenge if any CAPTCHA/2FA/bad-creds marker is on the page."""

    for key in CHALLENGE_KEYS:
        if driver.is_present(key, timeout_ms=1500):
            raise AuthChallenge(key)


def _verify(driver: PageDriver, gate: str, config: BrowserConfig) -> None:
    for key in VERIFY_GATES.get(gate, ()):
        if not driver.is_present(key, timeout_ms=config.default_timeout_ms):
            raise VerificationError(f"{gate}: anchor '{key}' never confirmed")


def login(driver: PageDriver, config: BrowserConfig, username: str,
          password: str) -> None:
    """Log in and confirm the app shell. Assumes storage_state may already be
    valid — if the app is already reachable, the credential path is skipped."""

    driver.goto(config.app_url)
    _detect_challenge(driver)
    if driver.is_present("app.ready", timeout_ms=4000):
        return  # a reused session is already authenticated

    driver.goto(config.login_url)
    if driver.is_present("consent.accept", timeout_ms=3000):
        driver.click("consent.accept")
    _detect_challenge(driver)

    step(driver, "login.username",
         lambda: driver.fill("login.username", username))
    step(driver, "login.password",
         lambda: driver.fill("login.password", password,
                             delay_ms=config.type_delay_ms))
    driver.click("login.submit")

    _detect_challenge(driver)  # OTP/CAPTCHA appears AFTER submit
    _verify(driver, "login", config)


def apply_filters(driver: PageDriver, config: BrowserConfig, county: str,
                  state: str) -> int | None:
    """Search the county, apply the vacant-land filter stack, verify, and
    return the live result count (fails closed if it exceeds max_export_rows)."""

    step(driver, "search.box",
         lambda: driver.fill("search.box", f"{county} County, {state.upper()}"))
    driver.click("search.submit")

    if driver.is_present("filters.open", timeout_ms=config.default_timeout_ms):
        driver.click("filters.open")

    # Property CLASS = Vacant Land (raw land), the load-bearing filter.
    driver.click("filters.property_class")
    driver.click("filters.property_class_option")

    if config.absentee_owner:
        driver.click("filters.absentee")
    if config.owner_occupied:
        driver.click("filters.owner_occupied")
    if config.tax_delinquent:
        driver.click("filters.tax_delinquent")
    _fill_if(driver, "filters.lot_min", config.lot_min_acres)
    _fill_if(driver, "filters.lot_max", config.lot_max_acres)
    _fill_if(driver, "filters.assessed_min", config.assessed_min_usd)
    _fill_if(driver, "filters.assessed_max", config.assessed_max_usd)
    _fill_if(driver, "filters.equity_min", config.equity_min_pct)

    driver.click("filters.apply")
    _verify(driver, "filters", config)

    count = driver.result_count("filters.result_count")
    # Fail CLOSED: an unreadable count (drifted 'filters.result_count' selector)
    # must NOT silently disengage the guardrail and let an unbounded skip-trace
    # (costs money per record) + export proceed. No count => refuse.
    if count is None:
        raise VerificationError(
            "could not read the result-count chip — calibrate "
            "'filters.result_count'; refusing to skip-trace/export without a "
            "confirmed row count (ToS + spend guardrail)")
    if count > config.max_export_rows:
        raise VerificationError(
            f"filtered count {count} exceeds max_export_rows "
            f"{config.max_export_rows} — narrow the filters; refusing to "
            "mass-export")
    return count


def _fill_if(driver: PageDriver, key: str, value) -> None:
    if value is not None:
        driver.fill(key, str(value))


def save_list(driver: PageDriver, config: BrowserConfig, county: str,
              state: str) -> None:
    """Select all results and persist them to a named list (skip-trace and
    export operate on a saved list)."""

    driver.click("results.select_all")
    driver.click("results.add_to_list")
    name = config.saved_list_name.format(
        county=county.lower(), state=state.lower(),
        date=datetime.now(timezone.utc).strftime("%Y%m%d"))
    driver.fill("list.name_input", name)
    driver.click("list.save")


def skiptrace(driver: PageDriver, config: BrowserConfig) -> None:
    """Run skip trace so the export carries Email/Phone columns. No-op (with a
    logged skip) when run_skiptrace is False."""

    if not config.run_skiptrace:
        return
    driver.click("skiptrace.button")
    if driver.is_present("skiptrace.confirm", timeout_ms=config.default_timeout_ms):
        driver.click("skiptrace.confirm")
    _verify(driver, "skiptrace", config)


def export(driver: PageDriver, config: BrowserConfig) -> str:
    """Export the list to CSV; return the downloaded file path.

    Menu shape (the researched PropStream flow): clicking Export opens a menu
    whose CSV item is the actual download trigger, so the download is armed
    around the CSV-item click, not the Export button. If the real UI downloads
    directly off the Export button, calibrate export.* on the VPS.
    """

    download_dir = str(Path(config.download_dir).expanduser())
    Path(download_dir).mkdir(parents=True, exist_ok=True)
    driver.click("export.button")   # open the export menu
    return driver.expect_download("export.confirm_csv", download_dir)


def move_to_inbox(src: str, county: str, state: str) -> Path:
    """Move (not copy) the export into the leads inbox so the intake watcher
    sees a settled file and takes over (suppress -> Instantly)."""

    from tools.integrations.lead_intake import INBOX

    INBOX.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    suffix = Path(src).suffix.lower() or ".csv"
    dest = INBOX / f"propstream-{county.lower()}-{state.lower()}-{stamp}{suffix}"
    shutil.move(src, dest)
    return dest


def run_pull(driver: PageDriver, config: BrowserConfig, county: str, state: str,
             *, username: str, password: str) -> Path:
    """Full flow: login -> filter -> save -> skiptrace -> export -> inbox."""

    login(driver, config, username, password)
    apply_filters(driver, config, county, state)
    save_list(driver, config, county, state)
    skiptrace(driver, config)
    exported = export(driver, config)
    return move_to_inbox(exported, county, state)
