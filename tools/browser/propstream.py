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

    # a still-valid session redirects login -> app; don't demand the form
    # when it never renders (observed live: slow app load misses the 4s
    # app.ready check above, then the login page bounces straight back)
    if not driver.is_present("login.username", timeout_ms=6000):
        if driver.is_present("app.ready", timeout_ms=10_000):
            return

    step(driver, "login.username",
         lambda: driver.fill("login.username", username))
    step(driver, "login.password",
         lambda: driver.fill("login.password", password,
                             delay_ms=config.type_delay_ms))
    driver.click("login.submit")

    _detect_challenge(driver)  # OTP/CAPTCHA appears AFTER submit
    # Single-session product rule: PropStream asks to end the other open
    # session ("Proceed") when this username is logged in elsewhere.
    if driver.is_present("session.proceed", timeout_ms=4000):
        driver.click("session.proceed")
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


# ---------------------------------------------------------------------------
# v2 flow — built from the live calibration of 2026-07-25/26 (every step
# below was individually proven against the real app; see browser.yaml notes)
# ---------------------------------------------------------------------------


def _count_from_view_button(driver) -> int | None:
    """'View 200,580 Properties' -> 200580 (the live filtered count)."""

    import re

    texts = getattr(driver, "visible_button_texts", lambda *_: [])(60)
    for t in texts:
        m = re.match(r"View\s+([\d,]+)\s+Propert", t or "")
        if m:
            return int(m.group(1).replace(",", ""))
    return None


def run_pull_v2(driver, config: BrowserConfig, county: str, state: str, *,
                username: str, password: str,
                recipe_steps=(), use_vacant_class: bool = True,
                lot_min_sqft: int | None = 5000,
                dry: bool = False, log=print) -> dict:
    """The calibrated pull. ``dry`` stops at the opened Actions menu —
    captures its items and commits nothing (a UI row-selection is the only
    side effect, and it vanishes with the session).

    Fail-closed everywhere: unreadable count, count over max_export_rows,
    or any missing anchor raises before anything is acted on. Skip trace
    (REAL MONEY per record) only runs when config.run_skiptrace and only
    under the same row cap.
    """

    report = {"county": county, "state": state, "count": None,
              "actions_menu": [], "downloaded": None, "dry": dry,
              "vacant_class_applied": False}

    login(driver, config, username, password)

    def _clear_overlays():
        for note in (getattr(driver, "dismiss_modals", list)() or []):
            log(f"[pull-v2] dismissed overlay: {note}")

    _clear_overlays()  # announcement/promo modals swallow pointer clicks

    # deterministic state: clear any persisted geography + filters
    getattr(driver, "click_any_containing", lambda w: "")(["Clear All"])

    # geography: type + select the autosuggest entry; the header Search
    # button is the executor fallback (both proven live)
    driver.click("search.box")
    getattr(driver, "type_keys", lambda t: None)(
        f"{county.title()} County, {state.upper()}")
    wait = getattr(driver, "wait_ms", lambda ms: None)
    picked = ""
    for _ in range(5):
        wait(1200)
        picked = getattr(driver, "find_and_click_suggestion",
                         lambda n: [])(f"{county.title()} County")
        if picked:
            break
    if not picked:
        getattr(driver, "click_text", lambda t: None)("Search")
    # geography proof: header counters leave 0/Loading (the search is async
    # and takes several seconds on a big county — real waits, not selector
    # probes that return instantly)
    for _ in range(20):
        wait(1500)
        counters = getattr(driver, "header_counters", list)()
        live = [c for c in counters
                if c and not c.startswith("0 ") and "Loading" not in c]
        if live:
            break
    else:
        raise VerificationError(
            f"county search never applied for {county}/{state} — counters "
            "stayed empty; refusing to continue")

    # filters: clean slate, then the recipe. A modal can appear AFTER the
    # search executes (observed live) — dismiss, and fall back to a JS
    # click (interception-immune) if the locator click is still blocked.
    _clear_overlays()
    try:
        driver.click("filters.open")
    except Exception:  # noqa: BLE001
        _clear_overlays()
        if not getattr(driver, "click_deep_text", lambda w: "")(["Filters"]):
            raise
    driver.is_present("filters.find", timeout_ms=4000)
    getattr(driver, "click_any_containing", lambda w: "")(["Clear Filter"])
    if use_vacant_class:
        # verify the chip actually applied: selecting Vacant Land reveals its
        # sub-classification chips ('Agricultural-Unimproved Vacant Land' —
        # screenshot-confirmed); retry once if the toggle missed
        for _ in range(2):
            getattr(driver, "click_after_heading", lambda *a: None)(
                "Property Classification(s)", "Vacant Land")
            wait(1200)
            if "Agricultural-Unimproved" in getattr(
                    driver, "page_text", lambda *_: "")(6000):
                report["vacant_class_applied"] = True
                break
        else:
            raise VerificationError(
                "Vacant Land classification did not apply (sub-chips never "
                "appeared) — refusing to pull the wrong property class")
    for label, min_v, max_v in recipe_steps:
        getattr(driver, "fill_labeled_range", lambda *a, **k: None)(
            label, min_v, max_v)
        getattr(driver, "press_key", lambda k: None)("Tab")
    if lot_min_sqft:
        getattr(driver, "fill_labeled_range", lambda *a, **k: None)(
            "Lot Size (SqFt)", lot_min_sqft, None)
    getattr(driver, "press_key", lambda k: None)("Tab")
    # THE GUARD: live count from the View button label, before anything is
    # selected. The label refreshes async after every filter edit (and can
    # briefly show the pre-filter count), so poll until two consecutive
    # reads agree. Fail closed on unreadable/over-cap.
    count = prev = None
    for _ in range(12):
        wait(1500)
        cur = _count_from_view_button(driver)
        if cur is not None and cur == prev:
            count = cur
            break
        prev = cur
    report["count"] = count
    if count is None:
        report["buttons_seen"] = getattr(driver, "visible_button_texts",
                                         list)(40)
        raise VerificationError(
            "could not read a stable live count from the View ... "
            "Properties button — refusing to select/export blind; "
            f"buttons seen: {report['buttons_seen']}")
    if count > config.max_export_rows:
        raise VerificationError(
            f"filtered count {count:,} exceeds max_export_rows "
            f"{config.max_export_rows:,} — narrow the recipe; refusing to "
            "mass-select (skip-trace/export cost + ToS guardrail)")
    log(f"[pull-v2] {county}/{state}: {count:,} properties within cap")

    # Commit the results view. Screenshot-proven: the synthetic View click
    # left the filters panel OPEN over the results toolbar — the grid loads
    # behind it (selection even works) but the covered Actions toggle can
    # never be clicked. Trusted click first, then verify the panel is gone
    # (Escape is the panel's close key; filters persist server-side).
    if not getattr(driver, "real_click_regex", lambda p: False)(
            r"View\s+[\d,]+\s+Propert"):
        getattr(driver, "click_button_containing", lambda w: "")(
            ["View", "Propert"])
    wait(4000)  # results panel loads its first page async
    for _ in range(3):
        if not driver.is_present("filters.find", timeout_ms=1500):
            break
        getattr(driver, "press_key", lambda k: None)("Escape")
        wait(1000)
    else:
        getattr(driver, "screenshot", lambda *_: "")("pull-v2-panel-stuck")
        raise VerificationError(
            "filters panel still covers the results view — refusing to "
            "click through it")

    import re

    def _selected_count():
        m = re.search(r"([\d,]+)\s+SELECTED",
                      getattr(driver, "page_text", lambda *_: "")(4000))
        return int(m.group(1).replace(",", "")) if m else None

    # Select all. The counter reads '0 SELECTED' until the selection actually
    # registers (bare 'SELECTED' in page text proves nothing), so poll for a
    # NON-ZERO count with real waits; one careful re-click if the first click
    # landed while the grid was still hydrating (re-clicking an unselected
    # grid is safe; a selected grid never reaches the re-click).
    if not getattr(driver, "click_first_checkbox", lambda: "")():
        raise VerificationError("results select-all checkbox not found")
    selected = 0
    for attempt in range(10):
        wait(1500)
        selected = _selected_count() or 0
        if selected:
            break
        if attempt == 4:
            getattr(driver, "click_first_checkbox", lambda: "")()
    if not selected:
        getattr(driver, "screenshot", lambda *_: "")("pull-v2-select-miss")
        raise VerificationError(
            "selection not confirmed (counter never left '0 SELECTED') — "
            "refusing to proceed to actions")
    report["selected"] = selected
    log(f"[pull-v2] {selected:,} rows selected")

    # The Actions dropdown holds Add to List / Skip Trace / Export. Proven
    # live: it's a bare div AND clicking the last 'Actions'-text match put
    # no 'Export' in the DOM at all — so a click alone proves nothing (a
    # grid column header can collide on the text, or the toggle may be
    # hover-driven). Try each Actions-text element (hover events + click),
    # newest-mounted first; the ONLY success signal is a visible Export.
    probe = getattr(driver, "text_probe", lambda *a: [])
    report["actions_candidates"] = probe("Actions", 8)
    report["actions_attempts"] = attempts = []

    def _export_visible() -> bool:
        # the JS probe can't pierce shadow DOM — the Playwright check can
        return (any(e.get("visible") for e in probe("Export", 5))
                or getattr(driver, "any_text_visible",
                           lambda t: False)("Export"))

    opened = ""
    # calibrated live 2026-07-27: the toggle is div.dropdownToggleBtn and it
    # IGNORES synthetic el.click() — only a trusted pointer sequence opens
    # it, so the real-events click is the primary
    for css in ('[class*="Results-style"][class*="dropdownToggleBtn"]',
                '[class*="dropdownToggleBtn"]'):
        for _ in range(3):
            ok = getattr(driver, "real_click_css", lambda c: False)(css)
            attempts.append(f"real_click_css({css}):{ok}")
            wait(1500)
            if ok and _export_visible():
                opened = "dropdownToggleBtn (real click)"
                break
        if opened:
            break
    for _ in range(2):
        if opened:
            break
        for idx in range(max(1, len(report["actions_candidates"]))):
            hit = getattr(driver, "click_nth_deep_text",
                          lambda w, i: "")(["Actions"], idx)
            attempts.append(f"deep_click[{idx}]:{hit or 'miss'}")
            if not hit:
                break
            wait(1500)
            if _export_visible():
                opened = hit
                break
        if opened:
            break
        wait(1500)
    report["menu_items"] = {
        label: {"probe": probe(label, 5),
                "locator_visible": getattr(driver, "any_text_visible",
                                           lambda t: False)(label)}
        for label in ("Export", "Skip Trace", "Add to List")}
    if not opened:
        # last diagnostic: open the menu one more time and dump what the
        # dropdown ACTUALLY contains — the items may carry labels none of
        # our guesses match
        getattr(driver, "real_click_css", lambda c: False)(
            '[class*="Results-style"][class*="dropdownToggleBtn"]')
        wait(1500)
        report["dropdown_dump"] = getattr(driver, "css_probe", lambda *a: [])(
            '[class*="ropdown"], [role="menu"] *, [class*="Results-style"] li',
            15)
        getattr(driver, "screenshot", lambda *_: "")("pull-v2-actions-miss")
        raise VerificationError(
            "no Actions click produced a visible 'Export' item — "
            f"dropdown dump: {report['dropdown_dump']} "
            f"attempts: {attempts}")
    log(f"[pull-v2] Actions menu opened via {opened} (Export visible)")
    getattr(driver, "screenshot", lambda *_: "")("pull-v2-actions-menu")

    if dry:
        log("[pull-v2] DRY RUN — Actions menu verified open, nothing acted on")
        return report

    # skip trace first when enabled (the export then carries contacts) —
    # real-events click first: the items share the toggle's dropdown
    if config.run_skiptrace:
        matched = (getattr(driver, "real_click_text", lambda t: False)(
                       "Skip Trace")
                   or getattr(driver, "click_any_containing", lambda w: "")(
                       ["Skip Trace"])
                   or getattr(driver, "click_deep_text", lambda w: "")(
                       ["Skip Trace"]))
        if matched:
            _detect_challenge(driver)
            if driver.is_present("skiptrace.confirm",
                                 timeout_ms=config.default_timeout_ms):
                driver.click("skiptrace.confirm")
            _verify(driver, "skiptrace", config)
            # reopen the menu for the export (trusted click on the toggle)
            getattr(driver, "real_click_css", lambda c: False)(
                '[class*="Results-style"][class*="dropdownToggleBtn"]')
            wait(1200)

    # export: menu item, then the CSV/confirm control triggers the download
    matched = (getattr(driver, "real_click_text", lambda t: False)("Export")
               or getattr(driver, "click_any_containing", lambda w: "")(
                   ["Export"])
               or getattr(driver, "click_deep_text", lambda w: "")(
                   ["Export"]))
    if not matched:
        raise VerificationError("Export action not found in the Actions menu")
    exported = getattr(driver, "download_by_words")(
        ["CSV"], str(Path(config.download_dir).expanduser()))
    dest = move_to_inbox(exported, county, state)
    report["downloaded"] = str(dest)
    log(f"[pull-v2] exported -> {dest}")
    return report
