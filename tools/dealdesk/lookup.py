"""tools/dealdesk/lookup.py — find a property in the PropStream data.

A seller says an address (loosely — "4210 Bering" not "4210 Bering Dr"); this
normalizes and matches it to a record, preferring the APN/parcel number when
given (the reliable key). Backed by the PropStream export via the existing
``leadfile`` parser, so it's offline-testable with real data. The freshest
source on the VPS is the Google Sheet Muffin syncs — a ``SheetLookup`` drops in
behind the same ``find`` interface later; for now the export file IS the source
(it's the same data Muffin syncs into the Sheet).

Fail-closed: an ambiguous or missing match returns ``None`` → the caller
escalates to a human rather than pricing a property it isn't sure about.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from tools.integrations.leadfile import parse_propstream

# Street-suffix noise stripped during address normalization.
_SUFFIXES = {
    "dr", "drive", "st", "street", "rd", "road", "ln", "lane", "ave", "avenue",
    "blvd", "boulevard", "ct", "court", "cir", "circle", "way", "pl", "place",
    "ter", "terrace", "hwy", "highway", "pkwy", "parkway", "trl", "trail",
}


def _to_float(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _norm_apn(apn: str | None) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", apn or "").upper()


def _norm_address(address: str | None) -> str:
    """Normalize a street address for loose matching (number + street stem)."""

    if not address:
        return ""
    text = re.sub(r"[^\w\s]", " ", address.lower())
    tokens = [t for t in text.split() if t not in _SUFFIXES]
    # Drop trailing unit/apt noise but keep the leading number + street tokens.
    return " ".join(tokens).strip()


@dataclass
class PropertyRecord:
    address: str | None
    city: str | None
    state: str | None
    zip: str | None
    county: str | None
    apn: str | None
    est_value: float | None
    assessed_value: float | None
    open_loans_balance: float | None
    lien_amount: float | None
    mls_status: str | None
    property_type: str | None
    raw: dict


def _to_record(row: dict) -> PropertyRecord:
    return PropertyRecord(
        address=row.get("Address"),
        city=row.get("City"),
        state=row.get("State"),
        zip=row.get("Zip"),
        county=row.get("County"),
        apn=row.get("APN"),
        est_value=_to_float(row.get("Est. Value")),
        assessed_value=_to_float(row.get("Total Assessed Value")),
        open_loans_balance=_to_float(row.get("Est. Remaining balance of Open Loans")),
        lien_amount=_to_float(row.get("Lien Amount")),
        mls_status=(row.get("MLS Status") or "").strip() or None,
        property_type=(row.get("Property Type") or "").strip() or None,
        raw=row,
    )


class FileLookup:
    """Property lookup backed by PropStream ``.xlsx``/``.csv`` export(s).

    ``path`` may be a single export file (the original single-market mode) or
    a DIRECTORY of per-market exports (e.g. ``harris_tx.xlsx`` +
    ``putnam_fl.xlsx``) — every export in the directory is merged into one
    index, so the desk can price any market's inbound call. On key collisions
    the first file (sorted by name) wins; per-market files keyed by market
    never collide in practice.
    """

    def __init__(self, path: str | Path):
        self.by_apn: dict[str, PropertyRecord] = {}
        self.by_addr: dict[str, PropertyRecord] = {}
        self.by_email: dict[str, PropertyRecord] = {}
        self.sources: list[str] = []
        root = Path(path)
        files = (
            sorted(p for p in root.iterdir()
                   if p.is_file() and p.suffix.lower() in (".xlsx", ".csv"))
            if root.is_dir() else [root]
        )
        if not files:
            raise FileNotFoundError(f"no lead exports in directory: {root}")
        for file in files:
            self._index(file)

    def _index(self, path: Path) -> None:
        self.sources.append(str(path))
        for row in parse_propstream(path):
            rec = _to_record(row)
            if rec.apn:
                self.by_apn.setdefault(_norm_apn(rec.apn), rec)
            key = _norm_address(rec.address)
            if key:
                self.by_addr.setdefault(key, rec)
            # Index the lead's emails so an inbound reply maps back to its lot.
            for i in range(1, 5):
                email = str(row.get(f"Email {i}", "") or "").strip().lower()
                if email:
                    self.by_email.setdefault(email, rec)

    def find_by_email(self, email: str | None) -> PropertyRecord | None:
        """Map an inbound reply's sender address back to their property record."""

        if not email:
            return None
        return self.by_email.get(email.strip().lower())

    def find(self, *, address: str | None = None, apn: str | None = None,
             owner_name: str | None = None) -> PropertyRecord | None:
        # 1. APN is the reliable key.
        if apn:
            rec = self.by_apn.get(_norm_apn(apn))
            if rec:
                return rec
        if not address:
            return None
        key = _norm_address(address)
        if not key:
            return None
        # 2. Exact normalized address.
        if key in self.by_addr:
            return self.by_addr[key]
        # 3. Loose match: same leading house-number + street stem is a prefix of
        #    exactly one record. Ambiguous (>1) or none -> None (escalate).
        hits = [rec for k, rec in self.by_addr.items()
                if k.startswith(key) or key.startswith(k)]
        return hits[0] if len(hits) == 1 else None
