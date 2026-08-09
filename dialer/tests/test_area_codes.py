"""The static NANP table: spot-check known codes, then validate the whole map."""

from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest

from orchestrator.compliance.area_codes import AREA_CODES, AreaCodeInfo, info_for_phone


@pytest.mark.parametrize(
    ("code", "state", "tz"),
    [
        ("212", "NY", "America/New_York"),
        ("614", "OH", "America/New_York"),
        ("213", "CA", "America/Los_Angeles"),
        ("808", "HI", "Pacific/Honolulu"),
        ("907", "AK", "America/Anchorage"),
        ("480", "AZ", "America/Phoenix"),
        ("520", "AZ", "America/Phoenix"),
        ("602", "AZ", "America/Phoenix"),
        ("623", "AZ", "America/Phoenix"),
        ("928", "AZ", "America/Phoenix"),
        ("915", "TX", "America/Denver"),      # El Paso: Mountain, not Central
        ("219", "IN", "America/Chicago"),     # NW Indiana: Central
        ("305", "FL", "America/New_York"),
    ],
)
def test_us_spot_checks(code: str, state: str, tz: str):
    info = AREA_CODES[code]
    assert info == AreaCodeInfo(state=state, tz=tz, country="US")


@pytest.mark.parametrize(
    ("code", "expected_zone"),
    [
        ("850", "America/New_York"),  # FL panhandle spans -> eastern zone
        ("812", "America/New_York"),  # southern Indiana spans -> eastern
        ("605", "America/Chicago"),   # South Dakota spans -> eastern (Central)
        ("208", "America/Denver"),    # Idaho spans -> eastern (Mountain)
        ("906", "America/New_York"),  # Michigan UP spans -> eastern
    ],
)
def test_spanning_codes_pick_eastern_zone(code: str, expected_zone: str):
    assert AREA_CODES[code].tz == expected_zone


@pytest.mark.parametrize("code", ["204", "416", "514", "604", "902", "867"])
def test_canadian_codes(code: str):
    assert AREA_CODES[code].country == "CA"


@pytest.mark.parametrize(
    "code",
    ["242", "441", "809", "868", "876",
     # US territories deliberately classified "other" (outside the audience).
     "787", "939", "340", "670", "671", "684"],
)
def test_caribbean_and_territories_are_other(code: str):
    assert AREA_CODES[code].country == "other"


def test_table_wide_invariants():
    assert len([i for i in AREA_CODES.values() if i.country == "US"]) >= 330
    for code, info in AREA_CODES.items():
        assert len(code) == 3 and code.isdigit(), code
        assert code[0] not in "01", f"{code}: NANP codes start with 2-9"
        assert info.country in ("US", "CA", "other"), code
        if info.country == "US":
            # Every US code must be dial-plannable: state + resolvable zone.
            assert info.state is not None, code
            assert info.tz is not None, code
        if info.tz is not None:
            ZoneInfo(info.tz)  # raises on a typo'd zone name


def test_tollfree_codes_are_absent_so_they_block():
    for code in ("800", "833", "844", "855", "866", "877", "888", "900"):
        assert code not in AREA_CODES


def test_info_for_phone():
    assert info_for_phone("+16145550100") == AREA_CODES["614"]
    assert info_for_phone("16145550100") == AREA_CODES["614"]
    assert info_for_phone("(614) 555-0100") == AREA_CODES["614"]
    assert info_for_phone("+19995550100") is None  # unknown code -> gate blocks
    assert info_for_phone("") is None
    assert info_for_phone("nonsense") is None
