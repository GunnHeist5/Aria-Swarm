"""Training Gym storage: problem sets, runs, attempts, grades.

Practice material is raw trainer content — never anonymized, never retrievable
by any tenant. It lives in its own database precisely so it can never walk near
knowledge.db's FTS index by accident. The only path from a gym miss to
production behavior is a knowledge.db draft that walks the scan+approve gate.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone

from app import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS gym_meta (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS problem_sets (
    set_id      TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    domain      TEXT,
    source_ref  TEXT,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS problems (
    problem_id  TEXT PRIMARY KEY,
    set_id      TEXT NOT NULL REFERENCES problem_sets(set_id),
    seq         INTEGER NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'conceptual' CHECK (kind IN ('conceptual','data')),
    prompt_md   TEXT NOT NULL,
    expected_steps_md TEXT,
    answer_key_md TEXT NOT NULL,
    dataset_filename TEXT,
    dataset_hash TEXT,
    domain      TEXT,
    status      TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','confirmed','retired')),
    version     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_problems_set ON problems(set_id, seq);
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    set_id      TEXT NOT NULL REFERENCES problem_sets(set_id),
    status      TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running','done')),
    note        TEXT,
    started_at  TEXT NOT NULL,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS attempts (
    attempt_id  TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL REFERENCES runs(run_id),
    problem_id  TEXT NOT NULL REFERENCES problems(problem_id),
    problem_version INTEGER NOT NULL,
    analysis_id TEXT,
    status      TEXT NOT NULL DEFAULT 'running'
                CHECK (status IN ('running','done','failed','refused')),
    answer_md   TEXT,
    transcript_json TEXT,
    tokens_in   INTEGER NOT NULL DEFAULT 0,
    tokens_out  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE (run_id, problem_id)
);
CREATE INDEX IF NOT EXISTS idx_attempts_run ON attempts(run_id);
CREATE TABLE IF NOT EXISTS grades (
    grade_id    TEXT PRIMARY KEY,
    attempt_id  TEXT NOT NULL UNIQUE REFERENCES attempts(attempt_id),
    verdict     TEXT NOT NULL CHECK (verdict IN ('pass','partial','fail','error')),
    score       REAL NOT NULL,
    gaps_json   TEXT NOT NULL,
    grader_raw  TEXT,
    trainer_override TEXT CHECK (trainer_override IN ('agree','grader_wrong','resolved')),
    override_note TEXT,
    correction_method_id TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id(prefix: str) -> str:
    return prefix + uuid.uuid4().hex[:12]


def connect() -> sqlite3.Connection:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.gym_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    return conn


# --- meta ------------------------------------------------------------------

def get_meta(key: str) -> str | None:
    with connect() as conn:
        row = conn.execute("SELECT value FROM gym_meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO gym_meta (key, value) VALUES (?,?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


# --- problem sets ----------------------------------------------------------

def create_problem_set(title: str, domain: str | None, source_ref: str | None) -> str:
    set_id = _id("ps_")
    with connect() as conn:
        conn.execute(
            "INSERT INTO problem_sets (set_id, title, domain, source_ref, created_at) VALUES (?,?,?,?,?)",
            (set_id, title, domain, source_ref, _now()),
        )
    return set_id


def get_problem_set(set_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM problem_sets WHERE set_id = ?", (set_id,)).fetchone()
    return dict(row) if row else None


def list_problem_sets() -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM problem_sets ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


# --- problems --------------------------------------------------------------

def add_problem(set_id: str, prompt_md: str, answer_key_md: str,
                expected_steps_md: str | None = None, kind: str = "conceptual",
                domain: str | None = None) -> str:
    if not get_problem_set(set_id):
        raise ValueError("unknown problem set")
    problem_id = _id("p_")
    now = _now()
    with connect() as conn:
        seq_row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS seq FROM problems WHERE set_id = ?", (set_id,)
        ).fetchone()
        conn.execute(
            "INSERT INTO problems (problem_id, set_id, seq, kind, prompt_md, expected_steps_md,"
            " answer_key_md, domain, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (problem_id, set_id, seq_row["seq"], kind, prompt_md, expected_steps_md,
             answer_key_md, domain, now, now),
        )
    return problem_id


def get_problem(problem_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM problems WHERE problem_id = ?", (problem_id,)).fetchone()
    return dict(row) if row else None


def list_problems(set_id: str, status: str | None = None) -> list[dict]:
    with connect() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM problems WHERE set_id = ? AND status = ? ORDER BY seq",
                (set_id, status),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM problems WHERE set_id = ? ORDER BY seq", (set_id,)
            ).fetchall()
    return [dict(r) for r in rows]


def attempt_count(problem_id: str) -> int:
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM attempts WHERE problem_id = ?", (problem_id,)
        ).fetchone()
    return row["n"]


def update_problem(problem_id: str, prompt_md: str | None = None,
                   expected_steps_md: str | None = None, answer_key_md: str | None = None,
                   kind: str | None = None, domain: str | None = None) -> None:
    problem = get_problem(problem_id)
    if not problem:
        raise ValueError("unknown problem")
    if problem["status"] == "confirmed" and attempt_count(problem_id) > 0:
        raise ValueError("attempted problems are immutable; retire and re-create instead")
    with connect() as conn:
        conn.execute(
            "UPDATE problems SET prompt_md = COALESCE(?, prompt_md),"
            " expected_steps_md = COALESCE(?, expected_steps_md),"
            " answer_key_md = COALESCE(?, answer_key_md),"
            " kind = COALESCE(?, kind), domain = COALESCE(?, domain), updated_at = ?"
            " WHERE problem_id = ?",
            (prompt_md, expected_steps_md, answer_key_md, kind, domain, _now(), problem_id),
        )


def attach_dataset(problem_id: str, filename: str, sha256: str) -> None:
    if not get_problem(problem_id):
        raise ValueError("unknown problem")
    with connect() as conn:
        conn.execute(
            "UPDATE problems SET dataset_filename = ?, dataset_hash = ?, updated_at = ? WHERE problem_id = ?",
            (filename, sha256, _now(), problem_id),
        )


def confirm_problem(problem_id: str) -> None:
    problem = get_problem(problem_id)
    if not problem:
        raise ValueError("unknown problem")
    if problem["kind"] == "data" and not problem["dataset_filename"]:
        raise ValueError("cannot confirm a data problem without an attached dataset")
    with connect() as conn:
        conn.execute(
            "UPDATE problems SET status = 'confirmed', updated_at = ? WHERE problem_id = ?",
            (_now(), problem_id),
        )


def retire_problem(problem_id: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE problems SET status = 'retired', updated_at = ? WHERE problem_id = ?",
            (_now(), problem_id),
        )


# --- runs / attempts -------------------------------------------------------

def create_run(set_id: str, note: str | None = None) -> str:
    if not get_problem_set(set_id):
        raise ValueError("unknown problem set")
    run_id = _id("r_")
    with connect() as conn:
        conn.execute(
            "INSERT INTO runs (run_id, set_id, note, started_at) VALUES (?,?,?,?)",
            (run_id, set_id, note, _now()),
        )
    return run_id


def get_run(run_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    return dict(row) if row else None


def list_runs(set_id: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM runs WHERE set_id = ? ORDER BY started_at", (set_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def finish_run(run_id: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE runs SET status = 'done', finished_at = ? WHERE run_id = ?",
            (_now(), run_id),
        )


def unattempted_problems(run_id: str) -> list[dict]:
    run = get_run(run_id)
    if not run:
        raise ValueError("unknown run")
    with connect() as conn:
        rows = conn.execute(
            "SELECT p.* FROM problems p"
            " LEFT JOIN attempts a ON a.problem_id = p.problem_id AND a.run_id = ?"
            " WHERE p.set_id = ? AND p.status = 'confirmed' AND a.attempt_id IS NULL"
            " ORDER BY p.seq",
            (run_id, run["set_id"]),
        ).fetchall()
    return [dict(r) for r in rows]


def create_attempt(run_id: str, problem_id: str, problem_version: int) -> str:
    attempt_id = _id("at_")
    with connect() as conn:
        conn.execute(
            "INSERT INTO attempts (attempt_id, run_id, problem_id, problem_version, created_at)"
            " VALUES (?,?,?,?,?)",
            (attempt_id, run_id, problem_id, problem_version, _now()),
        )
    return attempt_id


def finish_attempt(attempt_id: str, status: str, answer_md: str | None,
                   transcript_json: str | None, tokens_in: int, tokens_out: int,
                   analysis_id: str | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE attempts SET status = ?, answer_md = ?, transcript_json = ?,"
            " tokens_in = ?, tokens_out = ?, analysis_id = ?, completed_at = ?"
            " WHERE attempt_id = ?",
            (status, answer_md, transcript_json, tokens_in, tokens_out, analysis_id,
             _now(), attempt_id),
        )


def get_attempt(attempt_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
    return dict(row) if row else None


# --- grades ----------------------------------------------------------------

def add_grade(attempt_id: str, verdict: str, score: float, gaps_json: str,
              grader_raw: str | None = None) -> str:
    grade_id = _id("g_")
    now = _now()
    with connect() as conn:
        conn.execute(
            "INSERT INTO grades (grade_id, attempt_id, verdict, score, gaps_json, grader_raw,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (grade_id, attempt_id, verdict, score, gaps_json, grader_raw, now, now),
        )
    return grade_id


def get_grade(grade_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM grades WHERE grade_id = ?", (grade_id,)).fetchone()
    return dict(row) if row else None


def override_grade(grade_id: str, override: str, note: str | None = None) -> None:
    if override not in ("agree", "grader_wrong", "resolved"):
        raise ValueError("unknown override")
    with connect() as conn:
        conn.execute(
            "UPDATE grades SET trainer_override = ?, override_note = ?, updated_at = ? WHERE grade_id = ?",
            (override, note, _now(), grade_id),
        )


def set_grade_correction(grade_id: str, method_id: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE grades SET correction_method_id = ?, trainer_override = 'resolved', updated_at = ?"
            " WHERE grade_id = ?",
            (method_id, _now(), grade_id),
        )


# --- reporting -------------------------------------------------------------

def miss_queue(set_id: str | None = None) -> list[dict]:
    query = (
        "SELECT g.*, a.run_id, a.problem_id, a.answer_md, a.transcript_json, a.status AS attempt_status,"
        " p.prompt_md, p.answer_key_md, p.expected_steps_md, p.seq, p.set_id, p.domain"
        " FROM grades g"
        " JOIN attempts a ON a.attempt_id = g.attempt_id"
        " JOIN problems p ON p.problem_id = a.problem_id"
        " WHERE g.verdict IN ('partial','fail','error') AND g.trainer_override IS NULL"
    )
    params: tuple = ()
    if set_id:
        query += " AND p.set_id = ?"
        params = (set_id,)
    query += " ORDER BY g.created_at DESC"
    with connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def run_scorecard(set_id: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT r.run_id, r.status, r.started_at, r.finished_at,"
            " COUNT(g.grade_id) AS graded,"
            " SUM(CASE WHEN g.verdict = 'pass' THEN 1 ELSE 0 END) AS passes,"
            " SUM(CASE WHEN g.verdict = 'partial' THEN 1 ELSE 0 END) AS partials,"
            " SUM(CASE WHEN g.verdict IN ('fail','error') THEN 1 ELSE 0 END) AS fails,"
            " AVG(g.score) AS avg_score"
            " FROM runs r"
            " LEFT JOIN attempts a ON a.run_id = r.run_id"
            " LEFT JOIN grades g ON g.attempt_id = a.attempt_id"
            " WHERE r.set_id = ? GROUP BY r.run_id ORDER BY r.started_at",
            (set_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def run_progress(run_id: str) -> list[dict]:
    """Per-problem live state for one run: pending | running | done/failed/refused,
    with verdict/score once graded. Derived entirely from existing tables."""
    run = get_run(run_id)
    if not run:
        raise ValueError("unknown run")
    with connect() as conn:
        rows = conn.execute(
            "SELECT p.seq, p.problem_id,"
            " COALESCE(a.status, 'pending') AS status,"
            " a.created_at AS attempt_created_at,"
            " g.verdict, g.score"
            " FROM problems p"
            " LEFT JOIN attempts a ON a.problem_id = p.problem_id AND a.run_id = ?"
            " LEFT JOIN grades g ON g.attempt_id = a.attempt_id"
            " WHERE p.set_id = ? AND p.status = 'confirmed'"
            " ORDER BY p.seq",
            (run_id, run["set_id"]),
        ).fetchall()
    return [dict(r) for r in rows]


def domain_breakdown(run_id: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT COALESCE(p.domain, 'general') AS domain,"
            " COUNT(g.grade_id) AS graded,"
            " SUM(CASE WHEN g.verdict = 'pass' THEN 1 ELSE 0 END) AS passes,"
            " AVG(g.score) AS avg_score"
            " FROM attempts a"
            " JOIN problems p ON p.problem_id = a.problem_id"
            " LEFT JOIN grades g ON g.attempt_id = a.attempt_id"
            " WHERE a.run_id = ? GROUP BY COALESCE(p.domain, 'general') ORDER BY domain",
            (run_id,),
        ).fetchall()
    return [dict(r) for r in rows]
