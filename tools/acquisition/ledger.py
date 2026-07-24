"""tools/acquisition/ledger.py — the single source of truth for every lead.

SQLite at ``~/.automaton/acquisition.db`` (WAL, short-lived connections —
registry.py pattern). Every lead ever touched lives here, keyed by
``(county_key, apn)`` — e.g. ``("harris_tx", "0440240000280")`` — with its
full status history. Suppression and dedup are enforced HERE, before any
enrollment, not by convention in callers.

Status machine::

    new -> screened|killed -> traced -> enrolled -> replied
        -> negotiating -> offer_out -> contracted -> closed
    (dead / suppressed are terminal from anywhere)

Hard rule (enforced by :func:`assert_enrollable`): leads in ``negotiating``,
``offer_out`` or ``contracted`` are excluded from ALL campaign enrollment —
never cold-email someone mid-deal. STOP suppression writes through to the
legacy JSON opt-out store so the old loader path is blocked too.

DNC flags ride through untouched: phones are stored as JSON
``[{"number": ..., "dnc": bool}]`` exactly as flagged in the export.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

from ..integrations import suppression as legacy_suppression
from ..integrations.leadfile import parse_propstream  # noqa: F401  (re-export for callers)

STATUSES = (
    "new", "screened", "killed", "traced", "enrolled", "replied",
    "negotiating", "offer_out", "contracted", "closed", "dead", "suppressed",
)

# Enrollment while any of these is set is a bug in the caller, not a filter
# miss — assert_enrollable raises instead of skipping quietly.
HARD_EXCLUDED = ("negotiating", "offer_out", "contracted", "suppressed")

SUPPRESSION_KINDS = ("email", "phone", "apn")
SUPPRESSION_REASONS = ("STOP", "prior_negotiation", "manual")


class LedgerError(RuntimeError):
    """A ledger invariant was about to be violated. Fail loudly, never clamp."""


def _stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _month() -> str:
    return time.strftime("%Y-%m")


def db_path() -> Path:
    return Path(os.environ.get(
        "ACQUISITION_DB", os.path.expanduser("~/.automaton/acquisition.db")))


def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS leads (
        county_key TEXT NOT NULL,
        apn TEXT NOT NULL,
        county_name TEXT, state TEXT,
        owner_name TEXT, first_name TEXT, last_name TEXT,
        email TEXT, email2 TEXT,
        phones TEXT NOT NULL DEFAULT '[]',
        address TEXT, city TEXT, zip TEXT,
        lot_sqft REAL, est_value TEXT,
        source_list TEXT,
        status TEXT NOT NULL DEFAULT 'new',
        status_history TEXT NOT NULL DEFAULT '[]',
        verdict TEXT, mao REAL, open_at REAL, walk_at REAL,
        enrichment_source TEXT, screened_at TEXT,
        notes TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY (county_key, apn)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_leads_email ON leads(email)",
    "CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status)",
    """CREATE TABLE IF NOT EXISTS suppression (
        kind TEXT NOT NULL, value TEXT NOT NULL,
        reason TEXT NOT NULL, added_at TEXT NOT NULL,
        PRIMARY KEY (kind, value)
    )""",
    """CREATE TABLE IF NOT EXISTS quota (
        month TEXT PRIMARY KEY,
        exports_used INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS campaign_map (
        county_key TEXT PRIMARY KEY,
        campaign_id TEXT NOT NULL,
        recipe TEXT, domain_pool TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS review_queue (
        id TEXT PRIMARY KEY,
        lead_email TEXT NOT NULL,
        county_key TEXT, apn TEXT,
        all_matches TEXT NOT NULL DEFAULT '[]',
        eaccount TEXT, subject TEXT, ts TEXT,
        reply_text TEXT,
        classification TEXT, price_mentioned REAL,
        verification TEXT, draft TEXT, draft_kind TEXT,
        state TEXT NOT NULL DEFAULT 'new',
        reason TEXT, snooze_until TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )""",
)

# Columns added after the first live deployment — applied idempotently.
_MIGRATIONS = (
    "ALTER TABLE leads ADD COLUMN retail_estimate REAL",
    "ALTER TABLE leads ADD COLUMN evidence TEXT",
    # every export email (JSON list) — sellers reply from any of them; seen
    # live when replies from Email 3/4 addresses failed to match the ledger
    "ALTER TABLE leads ADD COLUMN emails TEXT",
)


def init_db(conn: sqlite3.Connection | None = None) -> None:
    own = conn is None
    conn = conn or connect()
    try:
        for stmt in _SCHEMA:
            conn.execute(stmt)
        for stmt in _MIGRATIONS:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass  # column already exists
        conn.commit()
    finally:
        if own:
            conn.close()


# ---------------------------------------------------------------------------
# Row keying
# ---------------------------------------------------------------------------


def county_key(row: dict) -> str | None:
    """``harris_tx`` from a PropStream row's County + State (else None)."""

    county = (row.get("County") or "").strip().lower().replace(" ", "_")
    state = (row.get("State") or "").strip().lower()
    return f"{county}_{state}" if county and state else None


def normalize_apn(row: dict) -> str | None:
    """The dedup key: APN uppercased/stripped; address-keyed when APN absent.

    A missing APN must not make a lead untrackable (or forever re-ingested),
    so those rows key on ``ADDR:<address>|<zip>`` — still stable across
    exports of the same parcel.
    """

    apn = (row.get("APN") or "").strip().upper().replace(" ", "")
    if apn:
        return apn
    addr = (row.get("Address") or "").strip().upper()
    if addr:
        return f"ADDR:{addr}|{(row.get('Zip') or '').strip()}"
    return None


def _phones(row: dict) -> str:
    """Phones 1-5 with their DNC flags, verbatim — flags ride through."""

    out = []
    for i in range(1, 6):
        number = (row.get(f"Phone {i}") or "").strip()
        if not number:
            continue
        dnc_raw = (row.get(f"Phone {i} DNC") or row.get(f"DNC {i}") or "").strip().lower()
        out.append({"number": number,
                    "dnc": dnc_raw in ("yes", "true", "y", "1", "t")})
    return json.dumps(out)


def _emails(row: dict) -> list[str]:
    found = []
    for col in ("Email 1", "Email 2", "Email 3", "Email 4"):
        value = (row.get(col) or "").strip().lower()
        if value and "@" in value and value not in found:
            found.append(value)
    return found


def _lot_sqft(row: dict) -> float | None:
    try:
        v = float(str(row.get("Lot Size Sqft")).replace(",", ""))
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------


def ingest_rows(rows: list[dict], *, source_list: str,
                mark_enrolled: bool = False,
                conn: sqlite3.Connection | None = None) -> dict:
    """Parse rows into the ledger: dedup against everything ever ingested.

    Existing ``(county_key, apn)`` rows are left untouched (first ingest
    wins) — re-ingesting the same export creates ZERO duplicates.

    ``mark_enrolled`` is for the one-time historical import: leads that have
    an email (the ones the old loader pushed) start life as ``enrolled``
    instead of ``new``, so the ledger reflects the campaign that is already
    running. Never use it for fresh pulls.
    """

    own = conn is None
    conn = conn or connect()
    report = {"rows": len(rows), "inserted": 0, "duplicate": 0, "no_key": 0,
              "source_list": source_list}
    now = _stamp()
    try:
        init_db(conn)
        for row in rows:
            ckey, apn = county_key(row), normalize_apn(row)
            if not ckey or not apn:
                report["no_key"] += 1
                continue
            all_emails = _emails(row)
            email = all_emails[0] if all_emails else None
            email2 = all_emails[1] if len(all_emails) > 1 else None
            status = "enrolled" if (mark_enrolled and email) else "new"
            history = json.dumps([{"at": now, "from": None, "to": status,
                                   "note": f"ingest:{source_list}"}])
            cur = conn.execute(
                """INSERT OR IGNORE INTO leads
                   (county_key, apn, county_name, state, owner_name,
                    first_name, last_name, email, email2, emails, phones,
                    address, city, zip, lot_sqft, est_value, source_list,
                    status, status_history, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (ckey, apn,
                 (row.get("County") or "").strip() or None,
                 (row.get("State") or "").strip() or None,
                 (row.get("Owner 1 Name") or row.get("Owner Name") or "").strip() or None,
                 (row.get("Owner 1 First Name") or "").strip() or None,
                 (row.get("Owner 1 Last Name") or "").strip() or None,
                 email, email2, json.dumps(all_emails), _phones(row),
                 (row.get("Address") or "").strip() or None,
                 (row.get("City") or "").strip() or None,
                 (row.get("Zip") or "").strip() or None,
                 _lot_sqft(row),
                 (row.get("Est. Value") or row.get("Total Assessed Value") or None),
                 source_list, status, history, now, now))
            if cur.rowcount:
                report["inserted"] += 1
            else:
                report["duplicate"] += 1
        conn.commit()
    finally:
        if own:
            conn.close()
    return report


# ---------------------------------------------------------------------------
# Status machine
# ---------------------------------------------------------------------------


def set_status(county: str, apn: str, status: str, *, note: str | None = None,
               conn: sqlite3.Connection | None = None) -> None:
    if status not in STATUSES:
        raise LedgerError(f"unknown status {status!r} (must be one of {STATUSES})")
    own = conn is None
    conn = conn or connect()
    try:
        row = conn.execute(
            "SELECT status, status_history FROM leads WHERE county_key=? AND apn=?",
            (county, apn)).fetchone()
        if row is None:
            raise LedgerError(f"no such lead: {county}/{apn}")
        history = json.loads(row["status_history"] or "[]")
        history.append({"at": _stamp(), "from": row["status"], "to": status,
                        "note": note})
        conn.execute(
            "UPDATE leads SET status=?, status_history=?, updated_at=? "
            "WHERE county_key=? AND apn=?",
            (status, json.dumps(history), _stamp(), county, apn))
        conn.commit()
    finally:
        if own:
            conn.close()


def write_enrichment(county: str, apn: str, *, verdict: str,
                     mao: float | None, open_at: float | None,
                     walk_at: float | None, source: str = "screener",
                     retail_estimate: float | None = None,
                     evidence: dict | None = None,
                     conn: sqlite3.Connection | None = None) -> None:
    """Write a screener/browsing result back and advance new -> screened/killed."""

    own = conn is None
    conn = conn or connect()
    try:
        init_db(conn)
        cur = conn.execute(
            "UPDATE leads SET verdict=?, mao=?, open_at=?, walk_at=?, "
            "retail_estimate=?, evidence=?, "
            "enrichment_source=?, screened_at=?, updated_at=? "
            "WHERE county_key=? AND apn=?",
            (verdict, mao, open_at, walk_at,
             retail_estimate, json.dumps(evidence) if evidence else None,
             source, _stamp(), _stamp(),
             county, apn))
        if not cur.rowcount:
            raise LedgerError(f"no such lead: {county}/{apn}")
        conn.commit()
    finally:
        if own:
            conn.close()
    new_status = "killed" if verdict == "PASS" else "screened"
    set_status(county, apn, new_status, note=f"enrich:{source}")


# ---------------------------------------------------------------------------
# Suppression
# ---------------------------------------------------------------------------


def suppress_value(kind: str, value: str, *, reason: str = "manual",
                   conn: sqlite3.Connection | None = None) -> int:
    """Add one suppression entry; mark matching leads ``suppressed``.

    Email suppressions write through to the legacy JSON opt-out store so the
    old ``instantly.py`` load path is blocked by the same STOP. Returns the
    number of ledger leads flipped to ``suppressed``.
    """

    if kind not in SUPPRESSION_KINDS:
        raise LedgerError(f"unknown suppression kind {kind!r}")
    if reason not in SUPPRESSION_REASONS:
        raise LedgerError(f"unknown suppression reason {reason!r}")
    value = value.strip().lower() if kind == "email" else value.strip()
    if not value:
        return 0

    own = conn is None
    conn = conn or connect()
    flipped = 0
    try:
        init_db(conn)
        conn.execute(
            "INSERT OR REPLACE INTO suppression (kind, value, reason, added_at) "
            "VALUES (?,?,?,?)", (kind, value, reason, _stamp()))
        if kind == "email":
            hits = conn.execute(
                "SELECT county_key, apn FROM leads WHERE lower(email)=? "
                "OR lower(email2)=? OR emails LIKE ?",
                (value, value, f'%"{value}"%')).fetchall()
        elif kind == "apn":
            hits = conn.execute(
                "SELECT county_key, apn FROM leads WHERE apn=?", (value.upper(),)).fetchall()
        else:
            hits = []
        conn.commit()
        for h in hits:
            set_status(h["county_key"], h["apn"], "suppressed",
                       note=f"suppression:{reason}", conn=conn)
            flipped += 1
    finally:
        if own:
            conn.close()

    if kind == "email":
        legacy_suppression.add(value)   # CAN-SPAM write-through, both paths
    return flipped


def suppressed_set(kind: str = "email",
                   conn: sqlite3.Connection | None = None) -> set[str]:
    own = conn is None
    conn = conn or connect()
    try:
        init_db(conn)
        rows = conn.execute(
            "SELECT value FROM suppression WHERE kind=?", (kind,)).fetchall()
    finally:
        if own:
            conn.close()
    values = {r["value"] for r in rows}
    if kind == "email":
        values |= legacy_suppression.load()
    return values


def assert_enrollable(lead: sqlite3.Row | dict, suppressed_emails: set[str]) -> None:
    """Raise ``LedgerError`` if enrolling this lead would violate a hard rule.

    This is the last line of defense — callers should have filtered already;
    reaching this with a bad lead is a bug that must be loud, not a lead that
    gets quietly skipped.
    """

    status = lead["status"]
    if status in HARD_EXCLUDED:
        raise LedgerError(
            f"HARD STOP: lead {lead['county_key']}/{lead['apn']} has status "
            f"{status!r} — mid-deal/suppressed leads must never enter a campaign")
    email = (lead["email"] or "").lower()
    if email and email in suppressed_emails:
        raise LedgerError(
            f"HARD STOP: lead {lead['county_key']}/{lead['apn']} email is "
            f"suppressed — must never be (re-)enrolled")


# ---------------------------------------------------------------------------
# Quota
# ---------------------------------------------------------------------------


def quota_record(exported: int, *, month: str | None = None,
                 conn: sqlite3.Connection | None = None) -> int:
    """Add ``exported`` rows to the month's usage; returns the new total."""

    month = month or _month()
    own = conn is None
    conn = conn or connect()
    try:
        init_db(conn)
        conn.execute(
            "INSERT INTO quota (month, exports_used) VALUES (?, ?) "
            "ON CONFLICT(month) DO UPDATE SET exports_used = exports_used + ?",
            (month, exported, exported))
        conn.commit()
        row = conn.execute(
            "SELECT exports_used FROM quota WHERE month=?", (month,)).fetchone()
        return int(row["exports_used"])
    finally:
        if own:
            conn.close()


def quota_used(month: str | None = None,
               conn: sqlite3.Connection | None = None) -> int:
    month = month or _month()
    own = conn is None
    conn = conn or connect()
    try:
        init_db(conn)
        row = conn.execute(
            "SELECT exports_used FROM quota WHERE month=?", (month,)).fetchone()
        return int(row["exports_used"]) if row else 0
    finally:
        if own:
            conn.close()


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def status_report(conn: sqlite3.Connection | None = None) -> dict:
    own = conn is None
    conn = conn or connect()
    try:
        init_db(conn)
        by_status = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) n FROM leads GROUP BY status")}
        by_county = {r["county_key"]: r["n"] for r in conn.execute(
            "SELECT county_key, COUNT(*) n FROM leads GROUP BY county_key")}
        suppression_n = conn.execute(
            "SELECT COUNT(*) n FROM suppression").fetchone()["n"]
    finally:
        if own:
            conn.close()
    return {"leads_total": sum(by_status.values()), "by_status": by_status,
            "by_county": by_county, "suppression_entries": suppression_n,
            "quota_used_this_month": quota_used()}
