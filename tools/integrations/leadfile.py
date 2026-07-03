"""tools/integrations/leadfile.py — parse PropStream exports into Instantly leads.

Stdlib-only reader for the PropStream property export (``.xlsx`` or ``.csv``)
plus the field map to Instantly lead payloads. Pure functions, no network —
`instantly.py` owns the push. PII stays in the rows; nothing here logs or
prints lead data (reporting is caller's responsibility, masked).

The ``.xlsx`` reader is deliberately minimal: PropStream writes every cell as
an inline string (``t="inlineStr"``), verified against a real export — no
openpyxl dependency needed on the VPS.
"""

from __future__ import annotations

import csv
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

SQFT_PER_ACRE = 43_560.0

# Export columns consulted by the field map / suppression (by header name).
EMAIL_COLUMNS = ("Email 1", "Email 2", "Email 3", "Email 4")


# ---------------------------------------------------------------------------
# File parsing
# ---------------------------------------------------------------------------


def _cell_value(cell) -> str | None:
    """Extract a cell's text: inline strings (PropStream) or plain <v> values."""

    if cell.get("t") == "inlineStr":
        text = "".join(t.text or "" for t in cell.iter(f"{_NS}t"))
        return text or None
    v = cell.find(f"{_NS}v")
    return v.text if v is not None and v.text != "" else None


def _col_index(ref: str) -> int:
    """'AB12' -> zero-based column index (27)."""

    letters = re.match(r"[A-Z]+", ref).group()
    idx = 0
    for ch in letters:
        idx = idx * 26 + (ord(ch) - 64)
    return idx - 1


def _parse_xlsx(path: Path) -> list[dict]:
    with zipfile.ZipFile(path) as zf:
        root = ET.fromstring(zf.read("xl/worksheets/sheet1.xml"))
    rows = root.find(f"{_NS}sheetData").findall(f"{_NS}row")
    if not rows:
        return []

    def row_values(row, ncols: int) -> list:
        out = [None] * ncols
        for cell in row.findall(f"{_NS}c"):
            i = _col_index(cell.get("r"))
            if i < ncols:
                out[i] = _cell_value(cell)
        return out

    ncols = max(_col_index(c.get("r")) for c in rows[0].findall(f"{_NS}c")) + 1
    headers = row_values(rows[0], ncols)
    return [
        {h: v for h, v in zip(headers, row_values(r, ncols)) if h}
        for r in rows[1:]
    ]


def _parse_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return [
            {k: (v or None) for k, v in row.items() if k}
            for row in csv.DictReader(fh)
        ]


def parse_propstream(path: str | Path) -> list[dict]:
    """Read a PropStream export (.xlsx or .csv) into header-keyed row dicts."""

    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"lead export not found: {path}")
    if path.suffix.lower() == ".xlsx":
        return _parse_xlsx(path)
    if path.suffix.lower() == ".csv":
        return _parse_csv(path)
    raise ValueError(f"unsupported lead export format: {path.suffix!r}")


# ---------------------------------------------------------------------------
# Field map -> Instantly lead payload
# ---------------------------------------------------------------------------


def _first_email(row: dict) -> str | None:
    for col in EMAIL_COLUMNS:
        value = (row.get(col) or "").strip()
        if value and "@" in value:
            return value.lower()
    return None


def _lot_acres(row: dict) -> float | None:
    raw = row.get("Lot Size Sqft")
    try:
        sqft = float(str(raw).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return round(sqft / SQFT_PER_ACRE, 2) if sqft > 0 else None


def _est_value(row: dict) -> str | None:
    # Prefer the modeled estimate; fall back to the assessed value.
    return row.get("Est. Value") or row.get("Total Assessed Value")


def to_instantly_lead(row: dict) -> dict | None:
    """Map one export row to an Instantly lead payload (None => not emailable).

    The custom_variables keys are the ``{{placeholders}}`` used by the campaign
    sequence. Blank first names stay blank — the sequence's fallback text
    handles the greeting, not fabricated data.
    """

    email = _first_email(row)
    if email is None:
        return None

    custom = {
        "propertyAddress": row.get("Address"),
        "propertyCity": row.get("City"),
        "propertyZip": row.get("Zip"),
        "county": row.get("County"),
        "state": row.get("State"),
        "apn": row.get("APN"),
        "lotAcres": _lot_acres(row),
        "estValue": _est_value(row),
    }
    return {
        "email": email,
        "first_name": (row.get("Owner 1 First Name") or "").strip(),
        "last_name": (row.get("Owner 1 Last Name") or "").strip(),
        "custom_variables": {k: v for k, v in custom.items() if v not in (None, "")},
    }


# ---------------------------------------------------------------------------
# Suppression
# ---------------------------------------------------------------------------


# MLS statuses that mean "owner already has an agent and/or a buyer" — cold
# outreach to these wastes sends and invites agent friction. EXPIRED/CANCELED/
# WITHDRAWN are kept on purpose (classic motivated-seller signals).
MLS_SUPPRESSED = {"active", "pending", "contingent"}


def suppress(rows: list[dict]) -> tuple[list[dict], dict]:
    """Apply the send-safety filters. Returns ``(kept_leads, report)``.

    Drops: rows with no usable email, PropStream-flagged litigators, parcels
    currently listed/under contract on MLS (``MLS_SUPPRESSED``), and duplicate
    emails (first occurrence wins). ``report`` counts each reason.
    """

    kept: list[dict] = []
    seen: set[str] = set()
    report = {
        "total_rows": len(rows),
        "no_email": 0,
        "litigator": 0,
        "mls_listed": 0,
        "duplicate_email": 0,
        "kept": 0,
    }

    for row in rows:
        if (row.get("Litigator") or "").strip().lower() in ("yes", "true", "y", "1"):
            report["litigator"] += 1
            continue
        if (row.get("MLS Status") or "").strip().lower() in MLS_SUPPRESSED:
            report["mls_listed"] += 1
            continue
        lead = to_instantly_lead(row)
        if lead is None:
            report["no_email"] += 1
            continue
        if lead["email"] in seen:
            report["duplicate_email"] += 1
            continue
        seen.add(lead["email"])
        kept.append(lead)

    report["kept"] = len(kept)
    return kept, report
