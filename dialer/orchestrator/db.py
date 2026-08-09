"""Postgres access: one process-wide pool, tiny helpers, SQL-file migrations.

The DB layer is deliberately synchronous (psycopg3 + ConnectionPool). Async
code (FastAPI handlers, arq jobs) wraps calls in `asyncio.to_thread(...)` —
at our concurrency (single-digit simultaneous calls) that is simpler and
safer than maintaining parallel sync/async stacks.

Rows come back as dicts (`dict_row`) so modules don't couple to column order.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from .config import Settings

_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

__all__ = [
    "Jsonb", "close_pool", "execute", "get_pool", "migrate",
    "query", "query_one", "tx",
]


def get_pool(cfg: Settings) -> ConnectionPool:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ConnectionPool(
                cfg.database_url,
                min_size=1,
                max_size=10,
                kwargs={"row_factory": dict_row},
                open=True,
            )
        return _pool


def close_pool() -> None:
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.close()
            _pool = None


@contextmanager
def tx(cfg: Settings) -> Iterator[psycopg.Connection]:
    """One connection, one transaction: commits on success, rolls back on error."""
    pool = get_pool(cfg)
    with pool.connection() as conn:
        with conn.transaction():
            yield conn


def query(cfg: Settings, sql: str, params: tuple | dict | None = None) -> list[dict[str, Any]]:
    with tx(cfg) as conn:
        return conn.execute(sql, params).fetchall()


def query_one(cfg: Settings, sql: str, params: tuple | dict | None = None) -> dict[str, Any] | None:
    rows = query(cfg, sql, params)
    return rows[0] if rows else None


def execute(cfg: Settings, sql: str, params: tuple | dict | None = None) -> int:
    """Run a statement in its own transaction; returns the affected row count."""
    with tx(cfg) as conn:
        cur = conn.execute(sql, params)
        return cur.rowcount


def migrate(cfg: Settings) -> list[str]:
    """Apply migrations/*.sql in filename order, exactly once each.

    Every cron/CLI entry point calls this before touching data, so a fresh
    database (or container) self-provisions.
    """
    applied: list[str] = []
    with tx(cfg) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                filename   TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        done = {
            row["filename"]
            for row in conn.execute("SELECT filename FROM schema_migrations").fetchall()
        }
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in done:
                continue
            conn.execute(path.read_text())  # type: ignore[arg-type]
            conn.execute(
                "INSERT INTO schema_migrations (filename) VALUES (%s)", (path.name,)
            )
            applied.append(path.name)
    return applied


# ---------------------------------------------------------------------------
# kv_state helpers — durable operational state (sync cursors, discovered
# company-field mapping, kill-switch persistence).


def kv_get(cfg: Settings, key: str) -> Any | None:
    row = query_one(cfg, "SELECT value FROM kv_state WHERE key = %s", (key,))
    return row["value"] if row else None


def kv_set(cfg: Settings, key: str, value: Any) -> None:
    execute(
        cfg,
        """
        INSERT INTO kv_state (key, value, updated_at) VALUES (%s, %s, now())
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
        """,
        (key, Jsonb(value)),
    )
