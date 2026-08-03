"""Layer 1: the global methods library.

Must never contain client-identifying content. Writes are open (ingest lands as
status='draft') but retrieval is gated twice: the FTS index is only populated on
approve, and search_methods() re-checks status='approved' AND anonymized=1 on
the joined row — so even a corrupted index cannot leak a draft.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone

from app import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS methods (
    method_id   TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    title       TEXT NOT NULL,
    body_md     TEXT NOT NULL,
    domain      TEXT,
    source_kind TEXT NOT NULL,
    source_ref  TEXT,
    status      TEXT NOT NULL DEFAULT 'draft'
                CHECK (status IN ('draft','pending_review','approved','rejected')),
    anonymized  INTEGER NOT NULL DEFAULT 0,
    scan_flags  TEXT,
    approved_by TEXT,
    approved_at TEXT,
    version     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS methods_fts USING fts5(method_id UNINDEXED, title, body_md);
CREATE TABLE IF NOT EXISTS trainer_sessions (
    session_id  TEXT PRIMARY KEY,
    title       TEXT,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trainer_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES trainer_sessions(session_id),
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect() -> sqlite3.Connection:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.knowledge_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    return conn


# --- methods lifecycle -----------------------------------------------------

def create_draft(kind: str, title: str, body_md: str, domain: str | None,
                 source_kind: str, source_ref: str | None = None) -> str:
    method_id = "m_" + uuid.uuid4().hex[:12]
    now = _now()
    with connect() as conn:
        conn.execute(
            "INSERT INTO methods (method_id, kind, title, body_md, domain, source_kind, source_ref,"
            " status, created_at, updated_at) VALUES (?,?,?,?,?,?,?, 'draft', ?, ?)",
            (method_id, kind, title, body_md, domain, source_kind, source_ref, now, now),
        )
    return method_id


def get_method(method_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM methods WHERE method_id = ?", (method_id,)).fetchone()
    return dict(row) if row else None


def list_methods(status: str | None = None) -> list[dict]:
    with connect() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM methods WHERE status = ? ORDER BY updated_at DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM methods ORDER BY updated_at DESC").fetchall()
    return [dict(r) for r in rows]


def update_draft(method_id: str, title: str | None = None, body_md: str | None = None,
                 scan_flags: str | None = None) -> None:
    method = get_method(method_id)
    if not method:
        raise ValueError("unknown method")
    if method["status"] == "approved":
        raise ValueError("approved methods are immutable; create a new version instead")
    with connect() as conn:
        conn.execute(
            "UPDATE methods SET title = COALESCE(?, title), body_md = COALESCE(?, body_md),"
            " scan_flags = COALESCE(?, scan_flags), updated_at = ? WHERE method_id = ?",
            (title, body_md, scan_flags, _now(), method_id),
        )


def approve(method_id: str, approved_by: str) -> None:
    """The gate. Requires anonymized=1 (set via mark_anonymized after human
    review). Publishing to FTS happens here and only here."""
    method = get_method(method_id)
    if not method:
        raise ValueError("unknown method")
    if not method["anonymized"]:
        raise ValueError("cannot approve: anonymization not confirmed")
    with connect() as conn:
        conn.execute(
            "UPDATE methods SET status = 'approved', approved_by = ?, approved_at = ?, updated_at = ?"
            " WHERE method_id = ?",
            (approved_by, _now(), _now(), method_id),
        )
        conn.execute("DELETE FROM methods_fts WHERE method_id = ?", (method_id,))
        conn.execute(
            "INSERT INTO methods_fts (method_id, title, body_md) VALUES (?,?,?)",
            (method_id, method["title"], method["body_md"]),
        )


def reject(method_id: str, rejected_by: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE methods SET status = 'rejected', approved_by = ?, updated_at = ? WHERE method_id = ?",
            (rejected_by, _now(), method_id),
        )
        conn.execute("DELETE FROM methods_fts WHERE method_id = ?", (method_id,))


def mark_anonymized(method_id: str, confirmed: bool) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE methods SET anonymized = ?, updated_at = ? WHERE method_id = ?",
            (1 if confirmed else 0, _now(), method_id),
        )


# --- retrieval -------------------------------------------------------------

def search_methods(query: str, limit: int = 3) -> list[dict]:
    """FTS bm25 over the published index, re-checked against the methods table.
    Only approved+anonymized rows are ever returned."""
    if not query.strip():
        return []
    # FTS5 query syntax chokes on raw punctuation; fall back to OR-joined words.
    terms = [w for w in "".join(c if c.isalnum() else " " for c in query).split() if w]
    if not terms:
        return []
    fts_query = " OR ".join(terms)
    with connect() as conn:
        rows = conn.execute(
            "SELECT m.* FROM methods_fts f"
            " JOIN methods m ON m.method_id = f.method_id"
            " WHERE methods_fts MATCH ? AND m.status = 'approved' AND m.anonymized = 1"
            " ORDER BY bm25(methods_fts) LIMIT ?",
            (fts_query, limit),
        ).fetchall()
    return [dict(r) for r in rows]


# --- trainer chat ----------------------------------------------------------

def create_trainer_session(title: str | None = None) -> str:
    session_id = "ts_" + uuid.uuid4().hex[:12]
    with connect() as conn:
        conn.execute(
            "INSERT INTO trainer_sessions (session_id, title, created_at) VALUES (?,?,?)",
            (session_id, title, _now()),
        )
    return session_id


def add_trainer_message(session_id: str, role: str, content: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO trainer_messages (session_id, role, content, created_at) VALUES (?,?,?,?)",
            (session_id, role, content, _now()),
        )


def list_trainer_messages(session_id: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM trainer_messages WHERE session_id = ? ORDER BY id", (session_id,)
        ).fetchall()
    return [dict(r) for r in rows]
