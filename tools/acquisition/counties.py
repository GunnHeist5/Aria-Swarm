"""tools/acquisition/counties.py — county expansion scoring + registry.

The rubric turns a candidate county into a 0-100 score; the agent RECOMMENDS,
the human picks. Inputs arrive pasted (`acquire counties add ... --growth-pct
2.4 ...`) or best-effort via the injected search seam (`--research`, Brave) —
extraction is conservative and fail-closed: an unparseable number stays NULL
and shows up as a gap in the scorecard, never a guess.

Registry: SQLite ``~/.automaton/counties.db`` (env ``COUNTIES_DB``).

Rubric (weights sum to 100 before the saturation penalty):
    growth        0-25   (5-yr population growth %, 5% caps it)
    band fit      0-20   (median lot value inside the $20-150K wholesale band)
    data avail    0-25   (CAD roll downloadable 10, GIS REST 10, tax online 5)
    dispo depth   0-30   (active land listings; 300+ caps it)
    saturation   -15     (guru-list counties everyone's mailing)
"""

from __future__ import annotations

import os
import re
import sqlite3
import time
from pathlib import Path

# Counties every land-flipping course tells beginners to mail — competition
# density is structurally high regardless of fundamentals. Hardcoded per the
# operator's live experience; extend as new guru darlings emerge.
GURU_COUNTIES = frozenset({
    "putnam_fl", "marion_fl", "polk_fl", "charlotte_fl", "lee_fl",
    "mohave_az", "cochise_az", "costilla_co", "valencia_nm",
    # far-west TX desert belt
    "hudspeth_tx", "culberson_tx", "presidio_tx", "brewster_tx",
    "jeff_davis_tx", "terrell_tx",
})

BAND_LOW, BAND_HIGH = 20_000, 150_000


def db_path() -> Path:
    return Path(os.environ.get(
        "COUNTIES_DB", os.path.expanduser("~/.automaton/counties.db")))


def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS counties (
            county_key TEXT PRIMARY KEY,
            growth_pct REAL, median_lot_value REAL,
            data_cad INTEGER, data_gis INTEGER, data_tax INTEGER,
            dispo_listings INTEGER,
            notes TEXT, updated_at TEXT
        )""")
    return conn


# ---------------------------------------------------------------------------
# Rubric (pure)
# ---------------------------------------------------------------------------


def score_county(c: dict) -> dict:
    """-> {"score", "parts", "gaps"} — NULL inputs score 0 and are reported."""

    parts: dict[str, float] = {}
    gaps: list[str] = []

    growth = c.get("growth_pct")
    if growth is None:
        gaps.append("growth_pct")
        parts["growth"] = 0.0
    else:
        parts["growth"] = max(0.0, min(25.0, float(growth) * 5.0))

    median = c.get("median_lot_value")
    if median is None:
        gaps.append("median_lot_value")
        parts["band_fit"] = 0.0
    elif BAND_LOW <= float(median) <= BAND_HIGH:
        parts["band_fit"] = 20.0
    elif BAND_LOW * 0.5 <= float(median) <= BAND_HIGH * 1.5:
        parts["band_fit"] = 10.0
    else:
        parts["band_fit"] = 0.0

    parts["data"] = (10.0 * bool(c.get("data_cad"))
                     + 10.0 * bool(c.get("data_gis"))
                     + 5.0 * bool(c.get("data_tax")))
    if not any(c.get(k) is not None for k in ("data_cad", "data_gis", "data_tax")):
        gaps.append("data availability")

    listings = c.get("dispo_listings")
    if listings is None:
        gaps.append("dispo_listings")
        parts["dispo"] = 0.0
    else:
        parts["dispo"] = min(30.0, float(listings) / 10.0)

    saturated = c["county_key"] in GURU_COUNTIES
    parts["saturation"] = -15.0 if saturated else 0.0

    return {"score": round(sum(parts.values()), 1), "parts": parts,
            "gaps": gaps, "saturated": saturated}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def upsert(county_key: str, conn: sqlite3.Connection | None = None,
           **fields) -> None:
    allowed = {"growth_pct", "median_lot_value", "data_cad", "data_gis",
               "data_tax", "dispo_listings", "notes"}
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"unknown county fields: {sorted(bad)}")
    own = conn is None
    conn = conn or connect()
    try:
        conn.execute(
            "INSERT INTO counties (county_key, updated_at) VALUES (?, ?) "
            "ON CONFLICT(county_key) DO NOTHING",
            (county_key, time.strftime("%Y-%m-%d %H:%M:%S")))
        for key, value in fields.items():
            if value is not None:
                conn.execute(
                    f"UPDATE counties SET {key}=?, updated_at=? WHERE county_key=?",
                    (value, time.strftime("%Y-%m-%d %H:%M:%S"), county_key))
        conn.commit()
    finally:
        if own:
            conn.close()


def scorecard(conn: sqlite3.Connection | None = None) -> list[dict]:
    """All candidates, scored and ranked (best first)."""

    own = conn is None
    conn = conn or connect()
    try:
        rows = [dict(r) for r in conn.execute("SELECT * FROM counties")]
    finally:
        if own:
            conn.close()
    out = []
    for row in rows:
        result = score_county(row)
        out.append({**row, **result})
    out.sort(key=lambda c: c["score"], reverse=True)
    return out


# ---------------------------------------------------------------------------
# Research seam (best-effort, fail-closed)
# ---------------------------------------------------------------------------

_PCT_RE = re.compile(r"(?:grew|growth|increased|up)[^.%]{0,40}?(\d{1,2}(?:\.\d+)?)\s*%",
                     re.I)


def research_county(county: str, state: str, *, search) -> dict:
    """Best-effort field fill via the injected search seam.

    ``search(query) -> list[{"title","description"}]`` (the screener's Brave
    client shape). Only unambiguous numbers are extracted; everything else
    lands in notes for the human. Never raises on empty results.
    """

    key = f"{county.lower()}_{state.lower()}"
    fields: dict = {"county_key": key, "growth_pct": None, "notes": ""}
    snippets = []
    try:
        for query in (f"{county} county {state} population growth percent",
                      f"{county} county {state} vacant land lots for sale"):
            for hit in (search(query) or [])[:5]:
                snippets.append(f"{hit.get('title', '')}: "
                                f"{hit.get('description') or hit.get('snippet', '')}")
    except Exception as exc:  # noqa: BLE001 — research is advisory, never fatal
        fields["notes"] = f"research failed: {exc}"
        return fields

    for snip in snippets:
        m = _PCT_RE.search(snip)
        if m:
            value = float(m.group(1))
            if 0 < value < 30:          # sanity: county growth, not a stat blob
                fields["growth_pct"] = value
                break
    fields["notes"] = " | ".join(snippets[:6])[:2000]
    return fields
