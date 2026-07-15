"""Offline tests for the browser runner — FakePageDriver, zero browser/network."""

from __future__ import annotations

import csv
from pathlib import Path

from tools.integrations import lead_intake

from . import propstream
from .calibrate import calibrate, validate_headers
from .config import DEFAULT_CONFIG, config_hash, load_config
from .driver import AuthChallenge, VerificationError
from .fake_driver import FakePageDriver

# The full set of anchors a clean happy-path run needs present.
HAPPY = {"app.ready", "filters.active_chip", "skiptrace.done",
         "filters.open", "consent.accept", "skiptrace.confirm",
         "export.confirm_csv"}
CFG = DEFAULT_CONFIG.mutate(type_delay_ms=0)


def _patch_inbox(tmp_path: Path) -> Path:
    inbox = tmp_path / "inbox"
    lead_intake.INBOX = inbox
    return inbox


def _driver(**kw) -> FakePageDriver:
    kw.setdefault("present_keys", HAPPY)
    kw.setdefault("download_path", "/tmp/aria_export_fixture.csv")
    # a readable, in-bounds result count (the guard fails closed without one)
    kw.setdefault("counts", {"filters.result_count": 240})
    return FakePageDriver(**kw)


def _make_export(path: str) -> None:
    Path(path).write_text(
        "Address,City,State,Zip,County,APN,Email 1,Lot Size Sqft\n"
        "1 Land Rd,Houston,TX,77028,Harris,044-000,x@y.com,21780\n",
        encoding="utf-8")


# ---------------------------------------------------------------------------
# happy path + structural fail-closed ordering
# ---------------------------------------------------------------------------


def test_run_pull_lands_in_inbox(tmp_path):
    inbox = _patch_inbox(tmp_path)
    src = tmp_path / "export.csv"
    _make_export(str(src))
    d = _driver(download_path=str(src))

    dest = propstream.run_pull(d, CFG, "harris", "tx", username="u", password="p")
    assert dest.parent == inbox and dest.exists()
    assert not src.exists()                       # moved, not copied
    assert dest.name.startswith("propstream-harris-tx-")


def test_export_never_precedes_verify_gates(tmp_path):
    _patch_inbox(tmp_path)
    src = tmp_path / "e.csv"
    _make_export(str(src))
    d = _driver(download_path=str(src))
    propstream.run_pull(d, CFG, "harris", "tx", username="u", password="p")

    dl = d.index_of("expect_download")
    # export must come after the login anchor AND the filter anchor confirmed
    login_ok = [i for i, c in enumerate(d.calls)
                if c[:2] == ("is_present", "app.ready")]
    filter_ok = [i for i, c in enumerate(d.calls)
                 if c[:2] == ("is_present", "filters.active_chip")]
    assert dl > login_ok[-1] and dl > filter_ok[-1]


def test_password_never_recorded(tmp_path):
    _patch_inbox(tmp_path)
    src = tmp_path / "e.csv"
    _make_export(str(src))
    d = _driver(download_path=str(src))
    propstream.run_pull(d, CFG, "harris", "tx", username="u",
                        password="SUPERSECRET")
    flat = repr(d.calls)
    assert "SUPERSECRET" not in flat            # value never enters the call log


# ---------------------------------------------------------------------------
# fail-closed cases
# ---------------------------------------------------------------------------


def test_login_unverified_never_exports(tmp_path):
    _patch_inbox(tmp_path)
    d = FakePageDriver(present_keys=set())       # no app.ready, no reused session
    try:
        propstream.run_pull(d, CFG, "harris", "tx", username="u", password="p")
        assert False, "expected VerificationError"
    except VerificationError:
        pass
    assert d.index_of("expect_download") == -1


def test_filters_unverified_stops_before_export(tmp_path):
    _patch_inbox(tmp_path)
    d = FakePageDriver(present_keys={"app.ready"})   # login ok, filters never confirm
    try:
        propstream.run_pull(d, CFG, "harris", "tx", username="u", password="p")
        assert False
    except VerificationError:
        pass
    assert d.index_of("expect_download") == -1


def test_auth_challenge_raises_before_login(tmp_path):
    _patch_inbox(tmp_path)
    d = FakePageDriver(present_keys={"challenge.otp"})
    try:
        propstream.login(d, CFG, "u", "p")
        assert False
    except AuthChallenge:
        pass
    assert d.index_of("expect_download") == -1


def test_row_cap_guard_blocks_mass_export(tmp_path):
    _patch_inbox(tmp_path)
    d = FakePageDriver(present_keys=HAPPY,
                       counts={"filters.result_count": 999_999})
    try:
        propstream.apply_filters(d, CFG.mutate(max_export_rows=5000), "harris", "tx")
        assert False
    except VerificationError as exc:
        assert "exceeds max_export_rows" in str(exc)


def test_row_cap_guard_fails_closed_on_unreadable_count(tmp_path):
    _patch_inbox(tmp_path)
    # filters verify, but the count chip is unreadable (None) -> must NOT export
    d = FakePageDriver(present_keys=HAPPY, counts={})  # result_count -> None
    try:
        propstream.apply_filters(d, CFG, "harris", "tx")
        assert False, "unreadable count must fail closed"
    except VerificationError as exc:
        assert "result-count" in str(exc)
    assert d.index_of("expect_download") == -1


def test_skiptrace_skipped_when_disabled(tmp_path):
    _patch_inbox(tmp_path)
    src = tmp_path / "e.csv"
    _make_export(str(src))
    cfg = CFG.mutate(run_skiptrace=False)
    # skiptrace.done intentionally absent; must still export
    d = _driver(present_keys=HAPPY - {"skiptrace.done"}, download_path=str(src))
    dest = propstream.run_pull(d, cfg, "harris", "tx", username="u", password="p")
    assert dest.exists()
    assert not d.clicked("skiptrace.button")


def test_reused_session_skips_credentials(tmp_path):
    _patch_inbox(tmp_path)
    # app.ready visible immediately => login() returns without touching creds
    d = FakePageDriver(present_keys={"app.ready"})
    propstream.login(d, CFG, "u", "p")
    assert not d.clicked("login.submit")


# ---------------------------------------------------------------------------
# calibration + header contract + config
# ---------------------------------------------------------------------------


def test_calibrate_reports_found_and_missing():
    d = FakePageDriver(present_keys={"login.username", "app.ready"})
    report = calibrate(d, CFG, keys=["login.username", "app.ready", "export.button"])
    assert set(report["found"]) == {"login.username", "app.ready"}
    assert report["missing"] == ["export.button"]
    assert not report["ok"]


def test_validate_headers_against_real_parser(tmp_path):
    good = tmp_path / "g.csv"
    _make_export(str(good))
    rep = validate_headers(str(good))
    assert rep["ok"] and rep["rows"] == 1 and rep["has_email_column"]

    with (tmp_path / "bad.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Foo", "Bar"])
        w.writerow(["1", "2"])
    bad = validate_headers(str(tmp_path / "bad.csv"))
    assert not bad["ok"] and "Address" in bad["missing_core"]


def test_load_config_yaml_overlay_and_selectors(tmp_path):
    p = tmp_path / "browser.yaml"
    p.write_text(
        "max_export_rows: 2000\n"
        "selectors:\n"
        "  export.button:\n"
        "    by: testid\n"
        "    id: real-export-btn\n",
        encoding="utf-8")
    cfg = load_config(str(p))
    assert cfg.max_export_rows == 2000
    from .config import load_selectors
    sel = load_selectors(str(p))
    assert sel["export.button"] == {"by": "testid", "id": "real-export-btn"}
    assert config_hash(cfg) != config_hash(DEFAULT_CONFIG)

    p.write_text("not_a_field: 1\n", encoding="utf-8")
    try:
        load_config(str(p))
        assert False
    except ValueError as exc:
        assert "not_a_field" in str(exc)


def test_real_driver_requires_session_without_playwright(tmp_path):
    # The missing-session guard must fire BEFORE Playwright is imported, so a
    # laptop-seeded session that never got shipped fails clearly (not with an
    # ImportError) even on a box where Playwright isn't installed.
    from tools.browser.driver import BrowserError
    from tools.browser.session import real_driver

    cfg = DEFAULT_CONFIG.mutate(storage_state=str(tmp_path / "absent.json"),
                                download_dir=str(tmp_path / "dl"),
                                artifact_dir=str(tmp_path / "art"))
    try:
        with real_driver(cfg):
            pass
        assert False, "expected BrowserError for a missing session"
    except BrowserError as exc:
        assert "storage_state" in str(exc) or "session" in str(exc)


def test_flow_imports_no_playwright():
    import sys

    # propstream/driver/config must import without playwright present
    for mod in ("tools.browser.propstream", "tools.browser.driver",
                "tools.browser.config", "tools.browser.fake_driver"):
        assert mod in sys.modules or __import__(mod)
    # the seam must not have leaked a playwright import into the flow module
    import tools.browser.propstream as ps
    assert "playwright" not in getattr(ps, "__dict__", {})


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
