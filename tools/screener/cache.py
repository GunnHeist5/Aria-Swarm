"""tools/screener/cache.py — SQLite response cache + resumability store.

Two tables in one zero-dependency DB (registry.py pattern: WAL, short-lived
connections, idempotent schema):

  * ``http_cache`` — raw 200 bodies keyed by a semantic key ("hcad:{acct}",
    "fema:{lat},{lon}", "brave:{sha1(q)}", ...). "Never re-fetch a parcel we
    already have" — survives --no-resume on purpose.
  * ``stage_results`` — the JSON payload each enrichment stage added to a row,
    keyed (account, stage) and stamped with the config genome hash, so a
    re-run skips completed work but a threshold change re-derives.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(
    os.environ.get("SCREENER_CACHE_DB", "~/.automaton/screener_cache.db")
).expanduser()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS http_cache (
    cache_key  TEXT PRIMARY KEY,
    status     INTEGER NOT NULL,
    body       TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stage_results (
    account     TEXT NOT NULL,
    stage       TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    payload     TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (account, stage)
);
"""


class Cache:
    """Handle to the screener cache DB (connections are per-operation)."""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path) if db_path else DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    # -- raw response cache ------------------------------------------------

    def get_http(self, key: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT body FROM http_cache WHERE cache_key = ?", (key,)
            ).fetchone()
        return row[0] if row else None

    def put_http(self, key: str, status: int, body: str) -> None:
        if status != 200:  # only cache successes — errors must be retryable
            return
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO http_cache VALUES (?, ?, ?, ?)",
                (key, status, body, datetime.now(timezone.utc).isoformat()),
            )

    # -- stage resumability --------------------------------------------------

    def get_stage(self, account: str, stage: str, cfg_hash: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM stage_results"
                " WHERE account = ? AND stage = ? AND config_hash = ?",
                (account, stage, cfg_hash),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def put_stage(self, account: str, stage: str, cfg_hash: str, payload: dict) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO stage_results VALUES (?, ?, ?, ?, ?)",
                (
                    account,
                    stage,
                    cfg_hash,
                    json.dumps(payload),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
