"""Per-tenant storage. Isolation lives or dies in this module.

Each tenant gets var/tenants/{tenant_id}/ (mode 0700) containing tenant.db,
uploads/, workspace/, deliverables/. `tenant_db()` is the ONLY function in the
codebase that opens a tenant database, and it refuses any tenant_id that fails
the regex or is absent from the control-plane tenants table — so a forged or
mistyped id can never build a filesystem path.
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app import config
from app.db import control

TENANT_ID_RE = re.compile(r"^t_[0-9a-f]{12}$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS datasets (
    dataset_id  TEXT PRIMARY KEY,
    filename    TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    rows        INTEGER,
    schema_json TEXT,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversations (
    conversation_id TEXT PRIMARY KEY,
    title       TEXT,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    tool_json   TEXT,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS analyses (
    analysis_id TEXT PRIMARY KEY,
    conversation_id TEXT,
    question    TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'running',
    summary     TEXT,
    created_at  TEXT NOT NULL,
    completed_at TEXT
);
CREATE TABLE IF NOT EXISTS deliverables (
    deliverable_id TEXT PRIMARY KEY,
    analysis_id TEXT REFERENCES analyses(analysis_id),
    kind        TEXT NOT NULL,
    title       TEXT,
    stored_path TEXT NOT NULL,
    mime        TEXT,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS learnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    text        TEXT NOT NULL,
    tags        TEXT,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_id TEXT NOT NULL,
    reviewer    TEXT NOT NULL,
    verdict     TEXT NOT NULL,
    correction_text TEXT,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS analysis_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_id TEXT NOT NULL REFERENCES analyses(analysis_id),
    kind        TEXT NOT NULL,
    text        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_analysis ON analysis_events(analysis_id, id);
CREATE TABLE IF NOT EXISTS analysis_partial (
    analysis_id TEXT PRIMARY KEY,
    text        TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
    document_id TEXT PRIMARY KEY,
    filename    TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    text        TEXT,
    text_chars  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS analysis_questions (
    question_id  TEXT PRIMARY KEY,
    analysis_id  TEXT NOT NULL REFERENCES analyses(analysis_id),
    question     TEXT NOT NULL,
    options_json TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS analysis_answers (
    question_id TEXT PRIMARY KEY,
    analysis_id TEXT NOT NULL,
    answer      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_tenant_id(tenant_id: str) -> str:
    """Regex + control-table existence, or raise. Belt and suspenders: the
    regex alone already excludes path traversal, but an id must also be a real,
    known tenant before it can touch the filesystem."""
    if not isinstance(tenant_id, str) or not TENANT_ID_RE.match(tenant_id):
        raise PermissionError("invalid tenant id")
    if control.get_tenant(tenant_id) is None:
        raise PermissionError("unknown tenant")
    return tenant_id


def tenant_dir(tenant_id: str) -> Path:
    validate_tenant_id(tenant_id)
    root = config.tenants_root() / tenant_id
    if not root.exists():
        root.mkdir(parents=True, mode=0o700)
        for sub in ("uploads", "workspace", "deliverables"):
            (root / sub).mkdir(mode=0o700)
    return root


def tenant_db(tenant_id: str) -> sqlite3.Connection:
    """The sole opener of tenant databases."""
    root = tenant_dir(tenant_id)
    conn = sqlite3.connect(root / "tenant.db")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    return conn


def resolve_tenant_file(tenant_id: str, stored_path: str) -> Path:
    """Resolve a DB-stored relative path against the tenant dir and verify it
    cannot escape. Every file response goes through here."""
    root = tenant_dir(tenant_id).resolve()
    resolved = (root / stored_path).resolve()
    if not resolved.is_relative_to(root):
        raise PermissionError("path escapes tenant directory")
    return resolved


# --- datasets --------------------------------------------------------------

def add_dataset(tenant_id: str, filename: str, stored_path: str, content_hash: str,
                rows: int | None, schema_json: str | None) -> str:
    dataset_id = "d_" + uuid.uuid4().hex[:12]
    with tenant_db(tenant_id) as conn:
        conn.execute(
            "INSERT INTO datasets (dataset_id, filename, stored_path, content_hash, rows, schema_json, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (dataset_id, filename, stored_path, content_hash, rows, schema_json, _now()),
        )
    return dataset_id


def list_datasets(tenant_id: str) -> list[dict]:
    with tenant_db(tenant_id) as conn:
        rows = conn.execute("SELECT * FROM datasets ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def get_dataset(tenant_id: str, dataset_id: str) -> dict | None:
    with tenant_db(tenant_id) as conn:
        row = conn.execute("SELECT * FROM datasets WHERE dataset_id = ?", (dataset_id,)).fetchone()
    return dict(row) if row else None


# --- conversations / messages ---------------------------------------------

def create_conversation(tenant_id: str, title: str | None = None) -> str:
    conversation_id = "c_" + uuid.uuid4().hex[:12]
    with tenant_db(tenant_id) as conn:
        conn.execute(
            "INSERT INTO conversations (conversation_id, title, created_at) VALUES (?,?,?)",
            (conversation_id, title, _now()),
        )
    return conversation_id


def list_conversations(tenant_id: str) -> list[dict]:
    with tenant_db(tenant_id) as conn:
        rows = conn.execute("SELECT * FROM conversations ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def add_message(tenant_id: str, conversation_id: str, role: str, content: str,
                tool_json: str | None = None) -> None:
    with tenant_db(tenant_id) as conn:
        conn.execute(
            "INSERT INTO messages (conversation_id, role, content, tool_json, created_at) VALUES (?,?,?,?,?)",
            (conversation_id, role, content, tool_json, _now()),
        )


def list_messages(tenant_id: str, conversation_id: str) -> list[dict]:
    with tenant_db(tenant_id) as conn:
        rows = conn.execute(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY id", (conversation_id,)
        ).fetchall()
    return [dict(r) for r in rows]


# --- analyses / deliverables / live events --------------------------------

def add_analysis_event(tenant_id: str, analysis_id: str, kind: str, text: str) -> None:
    with tenant_db(tenant_id) as conn:
        conn.execute(
            "INSERT INTO analysis_events (analysis_id, kind, text, created_at) VALUES (?,?,?,?)",
            (analysis_id, kind, text, _now()),
        )


def list_analysis_events(tenant_id: str, analysis_id: str, after_id: int = 0) -> list[dict]:
    with tenant_db(tenant_id) as conn:
        rows = conn.execute(
            "SELECT * FROM analysis_events WHERE analysis_id = ? AND id > ? ORDER BY id",
            (analysis_id, after_id),
        ).fetchall()
    return [dict(r) for r in rows]


def get_analysis(tenant_id: str, analysis_id: str) -> dict | None:
    with tenant_db(tenant_id) as conn:
        row = conn.execute(
            "SELECT * FROM analyses WHERE analysis_id = ?", (analysis_id,)
        ).fetchone()
    return dict(row) if row else None


def latest_running_analysis(tenant_id: str, conversation_id: str | None = None) -> dict | None:
    query = "SELECT * FROM analyses WHERE status IN ('running', 'awaiting_input')"
    params: tuple = ()
    if conversation_id:
        query += " AND conversation_id = ?"
        params = (conversation_id,)
    query += " ORDER BY created_at DESC LIMIT 1"
    with tenant_db(tenant_id) as conn:
        row = conn.execute(query, params).fetchone()
    return dict(row) if row else None


def set_analysis_status(tenant_id: str, analysis_id: str, status: str) -> None:
    """Status flip only (awaiting_input <-> running); finish_analysis owns the
    terminal states."""
    with tenant_db(tenant_id) as conn:
        conn.execute("UPDATE analyses SET status = ? WHERE analysis_id = ?", (status, analysis_id))


# --- streaming partial -------------------------------------------------------

def set_analysis_partial(tenant_id: str, analysis_id: str, text: str) -> None:
    with tenant_db(tenant_id) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO analysis_partial (analysis_id, text, updated_at) VALUES (?,?,?)",
            (analysis_id, text, _now()),
        )


def get_analysis_partial(tenant_id: str, analysis_id: str) -> str | None:
    with tenant_db(tenant_id) as conn:
        row = conn.execute(
            "SELECT text FROM analysis_partial WHERE analysis_id = ?", (analysis_id,)
        ).fetchone()
    return row["text"] if row else None


def clear_analysis_partial(tenant_id: str, analysis_id: str) -> None:
    with tenant_db(tenant_id) as conn:
        conn.execute("DELETE FROM analysis_partial WHERE analysis_id = ?", (analysis_id,))


# --- documents ---------------------------------------------------------------

def add_document(tenant_id: str, filename: str, stored_path: str,
                 text: str | None) -> str:
    document_id = "doc_" + uuid.uuid4().hex[:12]
    with tenant_db(tenant_id) as conn:
        conn.execute(
            "INSERT INTO documents (document_id, filename, stored_path, text, text_chars, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (document_id, filename, stored_path, text, len(text) if text else 0, _now()),
        )
    return document_id


def list_documents(tenant_id: str) -> list[dict]:
    with tenant_db(tenant_id) as conn:
        rows = conn.execute(
            "SELECT document_id, filename, text_chars, created_at FROM documents"
            " ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_document(tenant_id: str, document_id: str) -> dict | None:
    with tenant_db(tenant_id) as conn:
        row = conn.execute(
            "SELECT * FROM documents WHERE document_id = ?", (document_id,)
        ).fetchone()
    return dict(row) if row else None


# --- clarifying questions ------------------------------------------------------

def create_analysis_question(tenant_id: str, analysis_id: str, question: str,
                             options_json: str) -> str:
    question_id = "q_" + uuid.uuid4().hex[:12]
    with tenant_db(tenant_id) as conn:
        conn.execute(
            "INSERT INTO analysis_questions (question_id, analysis_id, question, options_json, created_at)"
            " VALUES (?,?,?,?,?)",
            (question_id, analysis_id, question, options_json, _now()),
        )
    return question_id


def get_analysis_question(tenant_id: str, question_id: str) -> dict | None:
    with tenant_db(tenant_id) as conn:
        row = conn.execute(
            "SELECT * FROM analysis_questions WHERE question_id = ?", (question_id,)
        ).fetchone()
    return dict(row) if row else None


def add_analysis_answer(tenant_id: str, question_id: str, analysis_id: str, answer: str) -> None:
    with tenant_db(tenant_id) as conn:
        conn.execute(
            "INSERT INTO analysis_answers (question_id, analysis_id, answer, created_at) VALUES (?,?,?,?)",
            (question_id, analysis_id, answer, _now()),
        )


def get_analysis_answer(tenant_id: str, question_id: str) -> dict | None:
    with tenant_db(tenant_id) as conn:
        row = conn.execute(
            "SELECT * FROM analysis_answers WHERE question_id = ?", (question_id,)
        ).fetchone()
    return dict(row) if row else None


def create_analysis(tenant_id: str, question: str, conversation_id: str | None = None) -> str:
    analysis_id = "a_" + uuid.uuid4().hex[:12]
    with tenant_db(tenant_id) as conn:
        conn.execute(
            "INSERT INTO analyses (analysis_id, conversation_id, question, status, created_at) VALUES (?,?,?,?,?)",
            (analysis_id, conversation_id, question, "running", _now()),
        )
    return analysis_id


def finish_analysis(tenant_id: str, analysis_id: str, status: str, summary: str | None) -> None:
    with tenant_db(tenant_id) as conn:
        conn.execute(
            "UPDATE analyses SET status = ?, summary = ?, completed_at = ? WHERE analysis_id = ?",
            (status, summary, _now(), analysis_id),
        )


def list_analyses(tenant_id: str) -> list[dict]:
    with tenant_db(tenant_id) as conn:
        rows = conn.execute("SELECT * FROM analyses ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def add_deliverable(tenant_id: str, analysis_id: str | None, kind: str, title: str,
                    stored_path: str, mime: str) -> str:
    resolve_tenant_file(tenant_id, stored_path)  # refuse escaping paths at write time too
    deliverable_id = "dl_" + uuid.uuid4().hex[:12]
    with tenant_db(tenant_id) as conn:
        conn.execute(
            "INSERT INTO deliverables (deliverable_id, analysis_id, kind, title, stored_path, mime, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (deliverable_id, analysis_id, kind, title, stored_path, mime, _now()),
        )
    return deliverable_id


def list_deliverables(tenant_id: str) -> list[dict]:
    with tenant_db(tenant_id) as conn:
        rows = conn.execute("SELECT * FROM deliverables ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def deliverables_for_conversation(tenant_id: str, conversation_id: str) -> list[dict]:
    with tenant_db(tenant_id) as conn:
        rows = conn.execute(
            "SELECT d.* FROM deliverables d"
            " JOIN analyses a ON a.analysis_id = d.analysis_id"
            " WHERE a.conversation_id = ? ORDER BY d.created_at",
            (conversation_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_deliverable(tenant_id: str, deliverable_id: str) -> dict | None:
    with tenant_db(tenant_id) as conn:
        row = conn.execute(
            "SELECT * FROM deliverables WHERE deliverable_id = ?", (deliverable_id,)
        ).fetchone()
    return dict(row) if row else None


# --- learnings (Layer 2) / reviews ----------------------------------------

def add_learning(tenant_id: str, source: str, text: str, tags: str | None = None) -> None:
    with tenant_db(tenant_id) as conn:
        conn.execute(
            "INSERT INTO learnings (source, text, tags, created_at) VALUES (?,?,?,?)",
            (source, text, tags, _now()),
        )


def recent_learnings(tenant_id: str, limit: int = 5) -> list[dict]:
    with tenant_db(tenant_id) as conn:
        rows = conn.execute(
            "SELECT * FROM learnings ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def add_review(tenant_id: str, analysis_id: str, reviewer: str, verdict: str,
               correction_text: str | None = None) -> None:
    with tenant_db(tenant_id) as conn:
        conn.execute(
            "INSERT INTO reviews (analysis_id, reviewer, verdict, correction_text, created_at) VALUES (?,?,?,?,?)",
            (analysis_id, reviewer, verdict, correction_text, _now()),
        )
    if verdict == "correct" and correction_text:
        add_learning(tenant_id, "correction", correction_text)
