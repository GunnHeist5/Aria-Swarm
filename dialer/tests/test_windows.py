"""Calling-window math: prospect-local, DST-correct, override-aware, fail-closed."""

from __future__ import annotations

import json
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from conftest import make_settings

from orchestrator.compliance import windows
from orchestrator.config import ConfigError

OHIO = "+16145550100"       # America/New_York
PHOENIX = "+16025550100"    # America/Phoenix (no DST)
UNKNOWN = "+19995550100"    # 999 is not an assigned area code

UTC = timezone.utc
EASTERN = ZoneInfo("America/New_York")


def write_windows(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "state_windows.json"
    path.write_text(json.dumps(payload))
    return path


# --- is_within_window -------------------------------------------------------

def test_within_window_winter(cfg):
    # 15:00 UTC = 10:00 EST
    assert windows.is_within_window(cfg, OHIO, datetime(2026, 1, 15, 15, 0, tzinfo=UTC))


def test_before_open_and_after_close(cfg):
    # 12:00 UTC = 07:00 EST
    assert not windows.is_within_window(cfg, OHIO, datetime(2026, 1, 15, 12, 0, tzinfo=UTC))
    # 23:30 UTC = 18:30 EST
    assert not windows.is_within_window(cfg, OHIO, datetime(2026, 1, 15, 23, 30, tzinfo=UTC))


def test_boundaries_start_inclusive_end_exclusive(cfg):
    # 14:00 UTC = 09:00:00 EST -> open
    assert windows.is_within_window(cfg, OHIO, datetime(2026, 1, 15, 14, 0, tzinfo=UTC))
    # 22:00 UTC = 17:00:00 EST -> already closed
    assert not windows.is_within_window(cfg, OHIO, datetime(2026, 1, 15, 22, 0, tzinfo=UTC))


def test_dst_shifts_the_utc_window(cfg):
    # Summer: 13:30 UTC = 09:30 EDT -> open; in winter the same UTC instant
    # would be 08:30 EST -> closed.
    assert windows.is_within_window(cfg, OHIO, datetime(2026, 7, 1, 13, 30, tzinfo=UTC))
    assert not windows.is_within_window(cfg, OHIO, datetime(2026, 1, 15, 13, 30, tzinfo=UTC))


def test_arizona_ignores_dst(cfg):
    # 16:30 UTC = 09:30 MST year-round
    assert windows.is_within_window(cfg, PHOENIX, datetime(2026, 7, 1, 16, 30, tzinfo=UTC))
    assert not windows.is_within_window(cfg, PHOENIX, datetime(2026, 7, 1, 15, 30, tzinfo=UTC))


def test_naive_now_is_treated_as_utc(cfg):
    assert windows.is_within_window(cfg, OHIO, datetime(2026, 1, 15, 15, 0))


def test_unknown_area_code_fails_closed(cfg):
    assert not windows.is_within_window(cfg, UNKNOWN, datetime(2026, 1, 15, 15, 0, tzinfo=UTC))
    assert windows.next_window_open(cfg, UNKNOWN, datetime(2026, 1, 15, 15, 0, tzinfo=UTC)) is None


# --- next_window_open -------------------------------------------------------

def test_next_open_same_day_before_start(cfg):
    now = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)  # 07:00 EST
    assert windows.next_window_open(cfg, OHIO, now) == datetime(
        2026, 1, 15, 9, 0, tzinfo=EASTERN
    )


def test_next_open_rolls_to_tomorrow_after_close(cfg):
    now = datetime(2026, 1, 15, 23, 30, tzinfo=UTC)  # 18:30 EST
    assert windows.next_window_open(cfg, OHIO, now) == datetime(
        2026, 1, 16, 9, 0, tzinfo=EASTERN
    )


def test_next_open_inside_window_is_now(cfg):
    now = datetime(2026, 1, 15, 15, 0, tzinfo=UTC)  # 10:00 EST
    assert windows.next_window_open(cfg, OHIO, now) == now


def test_next_open_across_dst_transition(cfg):
    # US DST 2026 starts Sunday March 8. Past close on the 7th rolls to
    # 09:00 on the 8th, which must carry the EDT (-4h) offset.
    now = datetime(2026, 3, 8, 4, 30, tzinfo=UTC)  # 23:30 EST Mar 7
    opens = windows.next_window_open(cfg, OHIO, now)
    assert opens == datetime(2026, 3, 8, 9, 0, tzinfo=EASTERN)
    assert opens.utcoffset() == timedelta(hours=-4)


# --- state override table ---------------------------------------------------

def test_state_override_narrows_the_window(tmp_path):
    path = write_windows(
        tmp_path,
        {"_comment": "test", "OH": {"start": "10:00", "end": "16:00"}},
    )
    cfg = make_settings(state_windows_path=str(path))
    assert windows.window_for_state(cfg, "oh") == (time(10, 0), time(16, 0))
    # Other states keep the defaults.
    assert windows.window_for_state(cfg, "FL") == (time(9, 0), time(17, 0))
    assert windows.window_for_state(cfg, None) == (time(9, 0), time(17, 0))
    # 14:30 UTC = 09:30 EST: inside the default window, outside the override.
    assert not windows.is_within_window(cfg, OHIO, datetime(2026, 1, 15, 14, 30, tzinfo=UTC))
    assert windows.next_window_open(
        cfg, OHIO, datetime(2026, 1, 15, 14, 30, tzinfo=UTC)
    ) == datetime(2026, 1, 15, 10, 0, tzinfo=EASTERN)


def test_shipped_starter_file_loads_with_no_active_overrides(cfg):
    # The repo's config/state_windows.json is documentation-only ("_" keys).
    for state in ("OH", "FL", "CA", "RI"):
        assert windows.window_for_state(cfg, state) == (time(9, 0), time(17, 0))


def test_missing_or_invalid_file_fails_closed(tmp_path):
    cfg = make_settings(state_windows_path=str(tmp_path / "nope.json"))
    with pytest.raises(ConfigError):
        windows.window_for_state(cfg, "OH")

    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(ConfigError):
        windows.window_for_state(make_settings(state_windows_path=str(bad)), "OH")


@pytest.mark.parametrize(
    "entry",
    [
        {"OH": {"start": "25:00", "end": "16:00"}},   # bad hour
        {"OH": {"start": "10:00"}},                   # missing end
        {"OH": {"start": "16:00", "end": "10:00"}},   # inverted window
        {"OH": "10:00-16:00"},                        # wrong shape
    ],
)
def test_malformed_override_entries_raise(tmp_path, entry):
    cfg = make_settings(state_windows_path=str(write_windows(tmp_path, entry)))
    with pytest.raises(ConfigError):
        windows.window_for_state(cfg, "OH")
