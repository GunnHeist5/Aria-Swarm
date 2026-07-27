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


def _click_text_robust(driver, text: str, wait=None) -> str:
    """Click an element by its text through every proven mechanism: trusted
    click, then tag-the-deepest + trusted click on the marker, then the
    React onClick of the marker's nearest handler-bearing ancestor.
    Returns which one worked ('' if none)."""

    if getattr(driver, "real_click_text", lambda t: False)(text):
        return "real_click_text"
    if getattr(driver, "tag_deepest_by_text", lambda *a: False)(
            text, "data-aria-hit"):
        if getattr(driver, "real_click_css", lambda c: False)(
                '[data-aria-hit="1"]'):
            return "tagged_click"
        res = getattr(driver, "react_invoke", lambda c, n: "")(
            '[data-aria-hit="1"]', "onClick")
        if res.startswith("invoked"):
            return res
    return ""


def _tag_actions_toggle(driver) -> str:
    """Marker selector for the element whose text IS 'Actions' (several
    dropdownToggleBtn siblings exist). Re-tag after every re-render."""

    if getattr(driver, "tag_element_by_text", lambda *a, **k: False)(
            '[class*="dropdownToggleBtn"]', "Actions"):
        return '[data-aria-target="1"]'
    return '[class*="dropdownToggleBtn"]'


def _open_actions_card(driver, wait) -> list:
    """Fire the toggle's onClick (its only handler) until the sibling
    dropdownCard mounts; returns the card's item texts."""

    for _ in range(4):
        getattr(driver, "react_invoke", lambda c, n: "")(
            _tag_actions_toggle(driver), "onClick")
        wait(900)
        items = [e.get("text", "") for e in
                 getattr(driver, "css_probe", lambda *a: [])(
                     '[class*="dropdownItem"]', 12)
                 if e.get("visible")]
        if items:
            return items
    return []


def _select_all_rows(driver, wait, log) -> int:
    """Click the grid's select-all and poll the 'N SELECTED' counter until
    it leaves zero. Returns the selected count (0 = never registered)."""

    import re

    if not getattr(driver, "click_first_checkbox", lambda: "")():
        return 0
    for attempt in range(10):
        wait(1500)
        m = re.search(r"([\d,]+)\s+SELECTED",
                      getattr(driver, "page_text", lambda *_: "")(4000))
        if m and int(m.group(1).replace(",", "")):
            return int(m.group(1).replace(",", ""))
        if attempt == 4:
            getattr(driver, "click_first_checkbox", lambda: "")()
    return 0


def run_export_list(driver, config: BrowserConfig, list_name: str, *,
                    username: str, password: str, county: str = "harris",
                    state: str = "tx", dry: bool = False, log=print) -> dict:
    """My Properties -> open a saved list -> select all -> Actions -> export
    the CSV into the leads inbox.

    ``dry`` stops once the list's Actions card is open, reporting its real
    items — nothing is clicked, nothing billable happens.
    """

    report = {"list_name": list_name, "selected": 0, "card_items": [],
              "downloaded": None, "dry": dry}
    wait = getattr(driver, "wait_ms", lambda ms: None)

    login(driver, config, username, password)
    for note in (getattr(driver, "dismiss_modals", list)() or []):
        log(f"[export] dismissed overlay: {note}")

    if not _click_text_robust(driver, "My Properties"):
        raise VerificationError("could not reach My Properties")
    wait(5000)
    report["screen"] = getattr(driver, "visible_own_texts", lambda *_: [])()[:30]
    if list_name not in report["screen"]:
        raise VerificationError(
            f"saved list {list_name!r} is not in My Properties — lists on "
            f"screen: {report['screen']}")

    how = _click_text_robust(driver, list_name)
    report["list_click"] = how
    if not how:
        getattr(driver, "screenshot", lambda *_: "")("export-list-click-miss")
        raise VerificationError(
            f"saved list {list_name!r} is present but would not open "
            "(no click mechanism worked)")
    wait(5000)
    report["list_screen"] = getattr(driver, "visible_own_texts",
                                    lambda *_: [])()[:30]

    report["selected"] = _select_all_rows(driver, wait, log)
    if not report["selected"]:
        getattr(driver, "screenshot", lambda *_: "")("export-select-miss")
        raise VerificationError(
            "select-all never registered on the list view — screen: "
            f"{report['list_screen']}")
    log(f"[export] {report['selected']:,} rows selected in {list_name!r}")

    report["card_items"] = _open_actions_card(driver, wait)
    getattr(driver, "screenshot", lambda *_: "")("export-actions-card")
    if not report["card_items"]:
        raise VerificationError("Actions card never opened on the list view")
    log(f"[export] Actions card: {report['card_items']}")

    if dry:
        log("[export] DRY RUN — card open, nothing clicked")
        return report

    export_label = next((t for t in report["card_items"]
                         if "export" in t.lower()), "")
    if not export_label:
        raise VerificationError(
            f"no export item in the list's Actions card: {report['card_items']}")

    # the export click may download directly or open a format dialog
    dest_dir = str(Path(config.download_dir).expanduser())
    try:
        exported = getattr(driver, "download_by_text")(export_label, dest_dir)
    except Exception:  # noqa: BLE001 — a dialog stands between click and file
        getattr(driver, "real_click_text", lambda t: False)(export_label)
        wait(2500)
        report["export_dialog"] = getattr(driver, "visible_own_texts",
                                          lambda *_: [])()[:25]
        getattr(driver, "screenshot", lambda *_: "")("export-dialog")
        exported = ""
        for label in ("CSV", "Export", "Download", "Confirm"):
            try:
                exported = getattr(driver, "download_by_text")(label, dest_dir)
                break
            except Exception:  # noqa: BLE001
                continue
        if not exported:
            raise VerificationError(
                "export clicked but no download started — dialog: "
                f"{report.get('export_dialog')}")
    dest = move_to_inbox(exported, county, state)
    report["downloaded"] = str(dest)
    log(f"[export] downloaded -> {dest}")
    return report


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

    # Commit the results view. Screenshot-proven: the panel left OPEN covers
    # the results toolbar — the grid loads behind it (selection even works)
    # but the Actions menu opens underneath the overlay. The open/closed
    # marker is the panel BODY text ('Lead Lists — Quickly search &
    # strategize...'): checking the find-input was a false negative, the
    # panel removes it on its own after the recipe fills.
    _view_re = r"View\s+[\d,]+\s+Propert"
    ok_view = getattr(driver, "real_click_regex", lambda p: False)(_view_re)
    report["view_click"] = ok_view
    if not ok_view:
        getattr(driver, "click_button_containing", lambda w: "")(
            ["View", "Propert"])
    wait(4000)  # results panel loads its first page async
    _panel_open = lambda: getattr(driver, "any_text_visible",  # noqa: E731
                                  lambda t: False)(
        "Quickly search & strategize")
    for i in range(5):
        if not _panel_open():
            break
        getattr(driver, "press_key", lambda k: None)("Escape")
        wait(1200)
        if i == 1:  # the first View click may have been swallowed — re-kick
            getattr(driver, "real_click_regex", lambda p: False)(_view_re)
            wait(3000)
    else:
        getattr(driver, "screenshot", lambda *_: "")("pull-v2-panel-stuck")
        raise VerificationError(
            "filters panel still covers the results view (panel body text "
            "remains visible) — refusing to click through it; "
            f"view_click={ok_view}")

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

    # The Actions dropdown — CALIBRATED 2026-07-27 from its wrapper HTML.
    # The toggle is div.dropdownToggleBtn whose text is 'Actions'; its
    # onClick (the ONLY handler) reveals a sibling div.dropdownCard holding
    # exactly two items: 'Input Range' and 'Save'. There is NO Export or
    # Skip Trace on the search grid — Save persists the selection to a list
    # in My Properties, where export + skip trace live. So the grid step is:
    # open the card, click Save, handle the save/name dialog.
    report["actions_attempts"] = attempts = []

    def _tag_toggle() -> str:
        if getattr(driver, "tag_element_by_text", lambda *a, **k: False)(
                '[class*="dropdownToggleBtn"]', "Actions"):
            return '[data-aria-target="1"]'
        return '[class*="Results-style"][class*="dropdownToggleBtn"]'

    toggle_css = _tag_toggle()
    report["toggle_css"] = toggle_css
    card_css = '[class*="dropdownCard"]'

    def _open_card() -> bool:
        for _ in range(4):
            getattr(driver, "react_invoke", lambda c, n: "")(
                _tag_toggle(), "onClick")
            wait(900)
            html = getattr(driver, "subtree_html", lambda *a: "")(
                card_css, 400)
            if html:
                return True
        return False

    if not _open_card():
        getattr(driver, "screenshot", lambda *_: "")("pull-v2-actions-miss")
        raise VerificationError(
            "Actions card never rendered after onClick — attempts: "
            f"{attempts}")
    report["actions_card_html"] = getattr(driver, "subtree_html",
                                          lambda *a: "")(card_css, 600)
    log("[pull-v2] Actions card open (items: Input Range, Save)")

    # click 'Save' — the dropdownItem, via its React onClick (the card items
    # are the same synthetic-click-deaf component family as the toggle)
    save_css = '[class*="dropdownItem"]'
    if not getattr(driver, "tag_element_by_text", lambda *a, **k: False)(
            save_css, "Save", "data-aria-save"):
        raise VerificationError(
            "no 'Save' item in the Actions card — card: "
            f"{report['actions_card_html']!r}")
    getattr(driver, "screenshot", lambda *_: "")("pull-v2-actions-menu")

    if dry:
        log("[pull-v2] DRY RUN — Actions card open, 'Save' located, "
            "nothing clicked")
        return report

    def _texts():
        return getattr(driver, "visible_own_texts", lambda *_: [])()

    def _click_item(label: str) -> str:
        """Tag the deepest element with this exact text and fire its React
        onClick (falling back to a trusted click)."""

        if not getattr(driver, "tag_deepest_by_text", lambda *a: False)(
                label, "data-aria-item"):
            return ""
        res = getattr(driver, "react_invoke", lambda c, n: "")(
            '[data-aria-item="1"]', "onClick")
        if res.startswith("invoked"):
            return res
        return ("real_click" if getattr(driver, "real_click_text",
                                        lambda t: False)(label) else "")

    before = set(_texts())
    invoked = getattr(driver, "react_invoke", lambda c, n: "")(
        '[data-aria-save="1"]', "onClick")
    attempts.append(f"save_onClick:{invoked}")
    wait(2000)
    # 'Save' opens the real action dialog — calibrated live 2026-07-27:
    #   Add to Marketing List | Skip Trace Selected Properties | Cancel
    save_diff = [t for t in _texts() if t not in before]
    report["after_save"] = save_diff[:25]
    log(f"[pull-v2] Save dialog: {save_diff[:15]}")

    # SKIP TRACE (real money, per record) — only on the explicit flag, and
    # only under the row cap already enforced above.
    if config.run_skiptrace:
        label = next((t for t in save_diff if "skip trace" in t.lower()),
                     "Skip Trace Selected Properties")
        log(f"[pull-v2] skip tracing {selected:,} records (BILLABLE)")
        res = _click_item(label)
        attempts.append(f"skiptrace:{res or 'miss'}")
        if not res:
            raise VerificationError(
                f"could not click {label!r} — dialog: {save_diff}")
        wait(3000)
        _detect_challenge(driver)
        report["after_skiptrace"] = [t for t in _texts()
                                     if t not in before][:25]
        getattr(driver, "screenshot", lambda *_: "")("pull-v2-skiptrace")
        log(f"[pull-v2] skip trace screen: {report['after_skiptrace'][:12]}")
        raise VerificationError(
            "skip-trace dialog reached — confirm step not yet calibrated; "
            f"screen: {report['after_skiptrace']}")

    # LIST PATH (free): Add to Marketing List -> name it -> confirm. The
    # export itself lives in My Properties on the saved list.
    label = next((t for t in save_diff if "marketing list" in t.lower()),
                 "Add to Marketing List")
    res = _click_item(label)
    attempts.append(f"add_to_list:{res or 'miss'}")
    if not res:
        getattr(driver, "screenshot", lambda *_: "")("pull-v2-dialog-miss")
        raise VerificationError(
            f"could not click {label!r} — dialog: {save_diff}")
    wait(2500)
    report["list_dialog"] = [t for t in _texts() if t not in before][:25]
    report["list_dialog_fields"] = getattr(driver, "dom_inventory",
                                           lambda *_: [])(25)
    getattr(driver, "screenshot", lambda *_: "")("pull-v2-list-dialog")

    # The modal (AddToMarketingListModal, calibrated live) holds a
    # ListManagementField combobox ('Select or Type to Create a New List'),
    # a checkbox, and Cancel/Save buttons — target the modal's OWN input and
    # button, never by bare text ('Save' also exists in the page header).
    list_name = config.saved_list_name.format(
        county=county, state=state,
        date=datetime.now(timezone.utc).strftime("%Y%m%d"))
    report["list_name"] = list_name
    filled = getattr(driver, "fill_css", lambda c, v: False)(
        '[class*="ListManagementField"] input', list_name)
    attempts.append(f"name_filled:{filled}")
    if not filled:
        raise VerificationError("could not type the list name into the modal")
    log(f"[pull-v2] naming the list {list_name!r}")
    wait(1200)
    # The combobox offers the typed name with a '(Create as New List)' tag —
    # that OPTION must be picked or Save has nothing selected to save.
    report["list_options"] = [t for t in _texts() if t not in before][:15]
    picked = ""
    for label in ("Create as New List", list_name):
        if getattr(driver, "real_click_text", lambda t: False)(label):
            picked = label
            break
    if not picked:  # comboboxes usually accept Enter as 'take what I typed'
        getattr(driver, "press_key", lambda k: None)("Enter")
        picked = "Enter"
    attempts.append(f"picked:{picked}")
    wait(1200)

    def _save_modal() -> bool:
        if not getattr(driver, "tag_element_by_text", lambda *a, **k: False)(
                '[class*="Modal"] button, [class*="modal"] button', "Save",
                "data-aria-confirm"):
            return False
        return bool(getattr(driver, "real_click_css", lambda c: False)(
                        '[data-aria-confirm="1"]')
                    or getattr(driver, "react_invoke", lambda c, n: "")(
                        '[data-aria-confirm="1"]',
                        "onClick").startswith("invoked"))

    # the modal CLOSING is the only success signal
    _open = lambda: getattr(driver, "any_text_visible",  # noqa: E731
                            lambda t: False)("Skip Trace Selected Properties")
    confirmed = _save_modal()
    attempts.append(f"modal_save:{confirmed}")
    wait(3500)
    if _open():  # second pass: re-affirm the option, then Save again
        getattr(driver, "press_key", lambda k: None)("Enter")
        wait(800)
        attempts.append(f"modal_save_retry:{_save_modal()}")
        wait(3500)
    report["after_list_save"] = [t for t in _texts() if t not in before][:25]
    getattr(driver, "screenshot", lambda *_: "")("pull-v2-list-saved")
    report["list_saved"] = not _open()
    if not report["list_saved"]:
        raise VerificationError(
            "list modal did not close after Save — screen: "
            f"{report['after_list_save']} attempts: {attempts}")
    log(f"[pull-v2] SAVED {selected:,} properties to list {list_name!r}")

    # The CSV export lives in My Properties on the saved list and is not
    # calibrated yet. Stop with the selection safely persisted (nothing
    # billable spent) rather than driving an unproven flow.
    raise VerificationError(
        f"selection saved to list {list_name!r} ({selected:,} properties) — "
        "the My Properties export leg is not automated yet; after-save "
        f"screen: {report['after_list_save']}")
