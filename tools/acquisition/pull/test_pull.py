"""Offline tests for the quota-tracked pull (M3) — FakePageDriver, no browser."""

from __future__ import annotations

import os
from pathlib import Path

from ...browser.config import DEFAULT_CONFIG as BROWSER_DEFAULTS
from ...browser.fake_driver import FakePageDriver
from tools.integrations import lead_intake

from .. import ledger
from ..config import DEFAULT_CONFIG
from . import propstream as pull
from .recipes import OWNERSHIP_YEARS_MIN, RECIPES, get_recipe, manual_checklist

HAPPY = {"app.ready", "filters.active_chip", "skiptrace.done",
         "filters.open", "consent.accept", "skiptrace.confirm",
         "export.confirm_csv"}
BCFG = BROWSER_DEFAULTS.mutate(type_delay_ms=0)


def _env(tmp_path: Path) -> None:
    os.environ["ACQUISITION_DB"] = str(tmp_path / "acq.db")
    lead_intake.INBOX = tmp_path / "inbox"
    pull.CHECKLIST_DIR = str(tmp_path / "checklists")


def _driver(tmp_path, count=240, present=HAPPY):
    src = tmp_path / "export.csv"
    src.write_text("Address,City,State,Zip,County,APN,Email 1,Lot Size Sqft\n"
                   "1 L Rd,Houston,TX,77028,Harris,044,x@y.com,21780\n",
                   encoding="utf-8")
    return FakePageDriver(present_keys=present, download_path=str(src),
                          counts={"filters.result_count": count})


# ---------------------------------------------------------------------------
# recipes
# ---------------------------------------------------------------------------


def test_every_recipe_enforces_ownership_length():
    for recipe in RECIPES.values():
        fills = {key: v[0] for a, key, *v in recipe.steps if a == "fill"}
        assert fills.get("filters.ownership_years_min") == str(OWNERSHIP_YEARS_MIN), \
            f"recipe {recipe.name} lost the ownership-length filter"


def test_recipe_registry_has_the_three_recipes():
    assert set(RECIPES) == {"vacant_land", "teardown_ratio", "commercial_vacant"}
    assert "20000" in str(get_recipe("teardown_ratio").steps)
    try:
        get_recipe("nope")
        assert False
    except ValueError as exc:
        assert "available" in str(exc)


def test_manual_checklist_is_complete():
    text = manual_checklist("harris", "tx", get_recipe("teardown_ratio"))
    assert "Harris County, TX" in text and "improvement" in text.lower()
    assert "leads_inbox" in text
    assert "80%" in text                       # the non-filterable criterion


# ---------------------------------------------------------------------------
# pacing
# ---------------------------------------------------------------------------


def test_paced_driver_sleeps_between_actions_and_caps(tmp_path):
    _env(tmp_path)
    naps = []
    d = pull.PacedDriver(_driver(tmp_path), DEFAULT_CONFIG.mutate(
        action_delay_min_s=2.0, action_delay_max_s=5.0,
        session_max_actions=3), sleep=naps.append)
    d.click("a")
    d.fill("b", "x")
    d.goto("http://c")
    assert len(naps) == 3
    assert all(2.0 <= n <= 5.0 for n in naps)  # human-like pacing bounds
    try:
        d.click("d")                            # 4th action busts the cap
        assert False
    except Exception as exc:  # noqa: BLE001
        assert "action cap" in str(exc)
    # non-action calls are never paced or capped
    d.is_present("whatever", timeout_ms=1)


# ---------------------------------------------------------------------------
# quota
# ---------------------------------------------------------------------------


def test_quota_guard_refuses_over_budget(tmp_path):
    _env(tmp_path)
    ledger.quota_record(49_900)
    try:
        pull.quota_guard(240, DEFAULT_CONFIG)
        assert False
    except Exception as exc:  # noqa: BLE001
        assert "quota" in str(exc)
    pull.quota_guard(50, DEFAULT_CONFIG)       # within remaining headroom


def test_successful_pull_records_quota_and_lands_in_inbox(tmp_path):
    _env(tmp_path)
    report = pull.run_recipe_pull(
        _driver(tmp_path), "harris", "tx", recipe_name="vacant_land",
        config=DEFAULT_CONFIG, browser_config=BCFG,
        username="u", password="p", sleep=lambda _s: None,
        log=lambda *_a: None)
    assert report["rows"] == 240 and report["stopped"] is None
    assert Path(report["inbox_file"]).exists()
    assert ledger.quota_used() == 240


def test_over_quota_pull_stops_before_export_with_checklist(tmp_path):
    _env(tmp_path)
    ledger.quota_record(49_900)                 # 100 remaining, count is 240
    d = _driver(tmp_path)
    report = pull.run_recipe_pull(
        d, "harris", "tx", config=DEFAULT_CONFIG, browser_config=BCFG,
        username="u", password="p", sleep=lambda _s: None,
        log=lambda *_a: None)
    assert report["inbox_file"] is None and "quota" in report["stopped"]
    assert Path(report["checklist"]).exists()
    assert d.index_of("expect_download") == -1  # export never armed
    assert ledger.quota_used() == 49_900        # nothing recorded


def test_challenge_wall_stops_and_writes_checklist_never_bypasses(tmp_path):
    _env(tmp_path)
    d = FakePageDriver(present_keys={"challenge.captcha"})
    hitl = []
    import tools.browser.session as session_mod

    original = getattr(session_mod, "freeze_hitl", None)
    session_mod.freeze_hitl = lambda msg: hitl.append(msg)
    try:
        report = pull.run_recipe_pull(
            d, "harris", "tx", recipe_name="commercial_vacant",
            config=DEFAULT_CONFIG, browser_config=BCFG,
            username="u", password="p", sleep=lambda _s: None,
            log=lambda *_a: None)
    finally:
        if original is not None:
            session_mod.freeze_hitl = original
    assert report["stopped"].startswith("challenge:")
    checklist = Path(report["checklist"]).read_text()
    assert "Manual PropStream pull" in checklist
    assert hitl and "challenge" in hitl[0].lower()
    assert d.index_of("expect_download") == -1


def test_custom_recipe_steps_are_driven_and_counted(tmp_path):
    _env(tmp_path)
    d = _driver(tmp_path)
    report = pull.run_recipe_pull(
        d, "harris", "tx", recipe_name="teardown_ratio",
        config=DEFAULT_CONFIG, browser_config=BCFG,
        username="u", password="p", sleep=lambda _s: None,
        log=lambda *_a: None)
    assert report["rows"] == 240
    assert d.clicked("filters.property_class_option_improved")
    flat = repr(d.calls)
    assert "filters.improvement_max" in flat
    assert "filters.ownership_years_min" in flat


if __name__ == "__main__":
    import sys
    import tempfile

    failures = 0
    module = sys.modules[__name__]
    for name in sorted(dir(module)):
        if name.startswith("test_"):
            fn = getattr(module, name)
            code = fn.__code__
            try:
                if "tmp_path" in code.co_varnames[: code.co_argcount]:
                    with tempfile.TemporaryDirectory() as td:
                        fn(Path(td))
                else:
                    fn()
                print(f"ok {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {exc}")
    raise SystemExit(1 if failures else 0)
