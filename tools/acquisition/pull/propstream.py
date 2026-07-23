"""tools/acquisition/pull/propstream.py — quota-tracked recipe pulls.

Wraps the existing browser runner (tools/browser — PageDriver seam, fail-
closed gates, HITL on challenge walls) with the acquisition layer's rules:

  * **Human-like pacing**: every driver action goes through PacedDriver,
    which sleeps 2-5 s (config) between actions and hard-caps actions per
    session. Deterministic jitter — no wall-clock randomness needed.
  * **Quota, fail-closed**: before export, the live result count is checked
    against the month's remaining 50K budget; after a successful export the
    usage is recorded in the ledger. Unreadable count already fails closed
    upstream.
  * **CAPTCHA/block => STOP**: AuthChallenge is never bypassed — the session
    freezes (Telegram HITL ping via the browser runner) and a manual pull
    checklist is written so the human can finish the pull by hand.
"""

from __future__ import annotations

import time
from pathlib import Path

from ...browser.config import BrowserConfig
from ...browser.config import DEFAULT_CONFIG as BROWSER_DEFAULTS
from ...browser.driver import AuthChallenge, PageDriver, VerificationError
from ...browser import propstream as flow
from .. import ledger
from ..config import AcquisitionConfig, DEFAULT_CONFIG
from .recipes import Recipe, get_recipe, manual_checklist

CHECKLIST_DIR = "~/.automaton/propstream/manual_checklists"


class PacedDriver:
    """PageDriver proxy that paces actions and caps them per session."""

    _PACED = ("goto", "click", "fill")

    def __init__(self, driver: PageDriver, config: AcquisitionConfig,
                 sleep=time.sleep):
        self._driver = driver
        self._config = config
        self._sleep = sleep
        self._actions = 0

    def _pace(self) -> None:
        self._actions += 1
        if self._actions > self._config.session_max_actions:
            raise VerificationError(
                f"session action cap ({self._config.session_max_actions}) "
                "reached — refusing to keep driving; re-run for the rest")
        lo, hi = self._config.action_delay_min_s, self._config.action_delay_max_s
        # deterministic jitter: cycles lo..hi in 7 steps (no Math.random needed)
        span = max(hi - lo, 0)
        self._sleep(lo + span * ((self._actions % 7) / 7))

    def __getattr__(self, name):
        attr = getattr(self._driver, name)
        if name in self._PACED and callable(attr):
            def paced(*args, **kwargs):
                self._pace()
                return attr(*args, **kwargs)

            return paced
        return attr


def apply_recipe(driver: PageDriver, browser_config: BrowserConfig,
                 recipe: Recipe, county: str, state: str) -> int:
    """Search + run the recipe's filter steps; return the confirmed count.

    Reuses the tested vacant-land flow for the builtin recipe; declarative
    step lists for the rest. Same fail-closed count guard either way.
    """

    if recipe.uses_builtin_filters:
        count = flow.apply_filters(driver, browser_config, county, state)
    else:
        driver.fill("search.box", f"{county} County, {state.upper()}")
        driver.click("search.submit")
        if driver.is_present("filters.open",
                             timeout_ms=browser_config.default_timeout_ms):
            driver.click("filters.open")
        for action, key, *value in recipe.steps:
            if action == "fill":
                driver.fill(key, value[0])
            else:
                driver.click(key)
        driver.click("filters.apply")
        flow._verify(driver, "filters", browser_config)
        count = driver.result_count("filters.result_count")
        if count is None:
            raise VerificationError(
                "could not read the result-count chip — calibrate "
                "'filters.result_count'; refusing to export blind")
        if count > browser_config.max_export_rows:
            raise VerificationError(
                f"filtered count {count} exceeds max_export_rows "
                f"{browser_config.max_export_rows} — narrow the recipe")
    # builtin path already ran the ownership step? No: it's part of the step
    # list — apply it for builtin recipes too (additive, harmless if already set)
    if recipe.uses_builtin_filters:
        for action, key, *value in recipe.steps:
            if action == "fill":
                driver.fill(key, value[0])
            else:
                driver.click(key)
        driver.click("filters.apply")
        flow._verify(driver, "filters", browser_config)
        recount = driver.result_count("filters.result_count")
        if recount is not None:
            count = recount
    return int(count)


def quota_guard(count: int, config: AcquisitionConfig) -> None:
    remaining = config.quota_monthly - ledger.quota_used()
    if count > remaining:
        raise VerificationError(
            f"pull of {count} rows would exceed the monthly export quota "
            f"({remaining} of {config.quota_monthly} remaining) — refusing; "
            "narrow filters or wait for the new month")


def write_checklist(county: str, state: str, recipe: Recipe,
                    reason: str) -> Path:
    out_dir = Path(CHECKLIST_DIR).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{county.lower()}_{state.lower()}_{recipe.name}.md"
    path.write_text(
        f"> automation stopped: {reason}\n\n"
        + manual_checklist(county, state, recipe),
        encoding="utf-8")
    return path


def run_recipe_pull(driver: PageDriver, county: str, state: str, *,
                    recipe_name: str = "vacant_land",
                    config: AcquisitionConfig = DEFAULT_CONFIG,
                    browser_config: BrowserConfig = BROWSER_DEFAULTS,
                    username: str, password: str,
                    sleep=time.sleep, log=print) -> dict:
    """The full quota-tracked pull. Returns a report; raises nothing for
    challenge walls (handled: HITL note + checklist + report)."""

    recipe = get_recipe(recipe_name)
    paced = PacedDriver(driver, config, sleep=sleep)
    report = {"county": county, "state": state, "recipe": recipe_name,
              "rows": None, "inbox_file": None, "stopped": None,
              "checklist": None}
    try:
        flow.login(paced, browser_config, username, password)
        count = apply_recipe(paced, browser_config, recipe, county, state)
        quota_guard(count, config)
        flow.save_list(paced, browser_config, county, state)
        flow.skiptrace(paced, browser_config)
        exported = flow.export(paced, browser_config)
        dest = flow.move_to_inbox(exported, county, state)
        used = ledger.quota_record(count)
        report.update(rows=count, inbox_file=str(dest))
        log(f"[pull] {county}/{state} {recipe_name}: {count} rows -> {dest} "
            f"(quota used this month: {used}/{config.quota_monthly})")
        return report
    except AuthChallenge as exc:
        # NEVER bypass. Freeze for the human, leave a hand-runnable checklist.
        checklist = write_checklist(county, state, recipe,
                                    f"challenge wall: {exc}")
        report.update(stopped=f"challenge:{exc}", checklist=str(checklist))
        try:
            from ...browser.session import freeze_hitl

            freeze_hitl(f"PropStream challenge during {recipe_name} pull for "
                        f"{county}/{state}: {exc}")
        except Exception as hitl_exc:  # noqa: BLE001
            log(f"[pull] HITL notify failed (checklist still written): {hitl_exc}")
        log(f"[pull] STOPPED at challenge wall — manual checklist: {checklist}")
        return report
    except VerificationError as exc:
        checklist = write_checklist(county, state, recipe, str(exc))
        report.update(stopped=str(exc), checklist=str(checklist))
        log(f"[pull] STOPPED: {exc} — manual checklist: {checklist}")
        return report
