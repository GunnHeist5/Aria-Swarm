"""US-biased NANP phone normalization.

Every phone number in the system is stored and compared as E.164
(`+1XXXXXXXXXX`). Normalizing at every boundary — sync, suppression writes,
DNC lookups, the dial gate — is what makes "suppress once, suppressed
everywhere" actually true: a number opt-ed out as "(614) 555-0100" must match
a dial attempt against "+16145550100".

Unparseable input returns None rather than raising: the caller decides what a
missing phone means (sync keeps the row; the gate blocks with INVALID_PHONE).
"""

from __future__ import annotations

import re

_NON_DIGITS = re.compile(r"\D")


def normalize_phone(raw: str) -> str | None:
    """Normalize to E.164 `+1XXXXXXXXXX`; None when unparseable.

    Accepts 10-digit national numbers, 11-digit with leading 1, +1 E.164, and
    any punctuation/spacing in between. Explicit non-NANP country codes
    (e.g. +44...) return None — this system dials NANP only.
    """
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip()
    if text.lower().startswith("tel:"):
        text = text[4:]
    digits = _NON_DIGITS.sub("", text)
    if text.startswith("+") and not digits.startswith("1"):
        return None  # explicit non-NANP country code
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return None
    # NANP area codes are N00-N99 with N in 2-9; a leading 0/1 is never a
    # valid US number, however it was formatted.
    if digits[0] in "01":
        return None
    return "+1" + digits


def last_ten_digits(raw: str) -> str | None:
    """The 10-digit national number, for matching against DNC-style lists.

    More forgiving than normalize_phone on purpose: DNC files come from third
    parties and may carry stray prefixes; when at least 10 digits are present
    the trailing 10 are the national number.
    """
    if not raw or not isinstance(raw, str):
        return None
    digits = _NON_DIGITS.sub("", raw)
    if len(digits) < 10:
        return None
    return digits[-10:]
