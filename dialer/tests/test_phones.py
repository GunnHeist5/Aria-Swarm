"""normalize_phone: every boundary of the system depends on this being strict."""

from __future__ import annotations

import pytest

from orchestrator.compliance.phones import last_ten_digits, normalize_phone


@pytest.mark.parametrize(
    "raw",
    [
        "6145550100",
        "16145550100",
        "+16145550100",
        "(614) 555-0100",
        "614-555-0100",
        "1-614-555-0100",
        "+1 (614) 555-0100",
        "  614.555.0100  ",
        "tel:+16145550100",
    ],
)
def test_normalizes_to_e164(raw: str):
    assert normalize_phone(raw) == "+16145550100"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "abc",
        "555-0100",             # too short
        "61455501001",          # 11 digits not starting with 1
        "+442071234567",        # explicit non-NANP country code
        "+1614555010",          # +1 but only 9 national digits
        "0145550100",           # NANP area codes never start with 0
        "1234567890",           # ...or 1
        "+11234567890",         # leading-1 area code hidden behind +1
        "614555010012",         # 12 digits
    ],
)
def test_unparseable_returns_none(raw: str):
    assert normalize_phone(raw) is None


def test_none_and_non_string_are_none():
    assert normalize_phone(None) is None  # type: ignore[arg-type]
    assert normalize_phone(6145550100) is None  # type: ignore[arg-type]


def test_last_ten_digits_is_forgiving():
    # DNC files carry stray prefixes; the trailing 10 digits are the number.
    assert last_ten_digits("+16145550100") == "6145550100"
    assert last_ten_digits("0016145550100") == "6145550100"
    assert last_ten_digits("(614) 555-0100") == "6145550100"
    assert last_ten_digits("555-0100") is None
    assert last_ten_digits("") is None
