"""Calling-window math in the PROSPECT's local time.

The window is a hard block: 9am-5pm local by default (already stricter than
the federal TCPA 8am-9pm rule), narrowable per state via
`config/state_windows.json`. Local time derives from the area code
(`area_codes.py`); an unknown zone means we cannot verify the window, so the
check fails closed (not-within, and no next-open time).

Weekends are ALLOWED by default — service-business owners answer their
business line on Saturdays, and SPEC §6 asks only for time-of-day limits. A
day-of-week restriction would be a new config knob, not a silent change here.

`now` is always an explicit parameter (never datetime.now() inside), so tests
and the dry-run simulator can evaluate any moment without freezing time.
Naive datetimes are treated as UTC.
"""

from __future__ import annotations

import json
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from ..config import ConfigError, Settings
from ..logging_utils import get_logger
from . import area_codes

log = get_logger(__name__)

# The overrides file is read per gate check; cache on (path, mtime) so an
# 8,000-contact dry run doesn't re-parse it 8,000 times, while an edited file
# is still picked up without a restart.
_cache: dict[str, tuple[float, dict[str, tuple[time, time]]]] = {}


def _parse_hhmm(text: str, *, context: str) -> time:
    try:
        hour, minute = text.strip().split(":")
        return time(int(hour), int(minute))
    except (ValueError, AttributeError) as exc:
        raise ConfigError(f"bad HH:MM value {text!r} in state_windows ({context})") from exc


def _load_state_windows(cfg: Settings) -> dict[str, tuple[time, time]]:
    """Parse the override table: {"XX": {"start": "HH:MM", "end": "HH:MM"}}.

    Top-level keys starting with "_" are documentation and ignored. A missing
    or malformed file raises ConfigError — the window table is compliance
    input, and fail-closed means refusing to run, not assuming defaults.
    """
    path = cfg.resolve_path(cfg.state_windows_path)
    try:
        mtime = path.stat().st_mtime
    except OSError as exc:
        raise ConfigError(f"state windows file missing/unreadable: {path}") from exc
    key = str(path)
    cached = _cache.get(key)
    if cached and cached[0] == mtime:
        return cached[1]

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"state windows file invalid JSON: {path}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"state windows file must be a JSON object: {path}")

    overrides: dict[str, tuple[time, time]] = {}
    for state, value in data.items():
        if state.startswith("_"):
            continue  # documentation keys
        if not isinstance(value, dict) or "start" not in value or "end" not in value:
            raise ConfigError(
                f"state_windows entry {state!r} must be {{'start': 'HH:MM', 'end': 'HH:MM'}}"
            )
        start = _parse_hhmm(str(value["start"]), context=state)
        end = _parse_hhmm(str(value["end"]), context=state)
        if start >= end:
            raise ConfigError(f"state_windows entry {state!r}: start must be before end")
        overrides[state.upper()] = (start, end)

    _cache[key] = (mtime, overrides)
    return overrides


def window_for_state(cfg: Settings, state: str | None) -> tuple[time, time]:
    """The (start, end) local-time window for a state; defaults when no override."""
    if state:
        override = _load_state_windows(cfg).get(state.upper())
        if override:
            return override
    return (cfg.call_window_start, cfg.call_window_end)


def _local_now(phone_e164: str, now: datetime) -> tuple[datetime, ZoneInfo, str | None] | None:
    """Resolve `now` into the prospect's zone; None when the zone is unknown."""
    info = area_codes.info_for_phone(phone_e164)
    if info is None or info.tz is None:
        return None
    try:
        zone = ZoneInfo(info.tz)
    except Exception:  # bad tz string in the table — fail closed, loudly
        log.error("unresolvable timezone %r for area code of %s…", info.tz, phone_e164[:5])
        return None
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(zone), zone, info.state


def is_within_window(cfg: Settings, phone_e164: str, now: datetime) -> bool:
    """True when `now` falls in the prospect's local window. Unknown zone -> False."""
    resolved = _local_now(phone_e164, now)
    if resolved is None:
        return False
    local, _zone, state = resolved
    start, end = window_for_state(cfg, state)
    # Start inclusive, end exclusive: a 17:00:00 dial on a 09:00-17:00 window
    # is already outside.
    return start <= local.time() < end


def next_window_open(cfg: Settings, phone_e164: str, now: datetime) -> datetime | None:
    """The next moment the prospect's window is open, in their local zone.

    Already inside the window -> `now` itself (as an aware datetime). Past
    close -> tomorrow at open. Unknown zone -> None (caller must treat the
    number as unschedulable, not guess).
    """
    resolved = _local_now(phone_e164, now)
    if resolved is None:
        return None
    local, zone, state = resolved
    start, end = window_for_state(cfg, state)
    if start <= local.time() < end:
        return local
    open_day = local.date() if local.time() < start else local.date() + timedelta(days=1)
    # combine() with tzinfo localizes the wall-clock time correctly across DST
    # transitions (zoneinfo picks the right offset for that date).
    return datetime.combine(open_day, start, tzinfo=zone)
