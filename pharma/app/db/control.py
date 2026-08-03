"""Control plane: tenants, API keys, usage metering, audit log.

Holds metadata only — no client data content ever lands here. Audit rows carry
ids and hashes, never payloads. Connections are short-lived (open, do the work,
close) so cron/CLI/web can all touch the DB without a daemon.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import uuid
from datetime import datetime, timezone

from app import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    tenant_id   TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    tier        TEXT NOT NULL DEFAULT 'starter',
    status      TEXT NOT NULL DEFAULT 'active',
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS api_keys (
    key_id      TEXT PRIMARY KEY,
    key_hash    TEXT NOT NULL UNIQUE,
    tenant_id   TEXT REFERENCES tenants(tenant_id),
    role        TEXT NOT NULL CHECK (role IN ('client','trainer','admin')),
    label       TEXT,
    created_at  TEXT NOT NULL,
    revoked_at  TEXT
);
CREATE TABLE IF NOT EXISTS usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id   TEXT NOT NULL,
    kind        TEXT NOT NULL,
    tokens_in   INTEGER,
    tokens_out  INTEGER,
    model       TEXT,
    est_cost_usd REAL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id   TEXT,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    resource    TEXT,
    detail      TEXT,
    created_at  TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect() -> sqlite3.Connection:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.control_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    return conn


def hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


# --- tenants ---------------------------------------------------------------

def create_tenant(name: str, tier: str = "starter") -> str:
    if tier not in config.TIERS:
        raise ValueError(f"unknown tier: {tier}")
    tenant_id = "t_" + uuid.uuid4().hex[:12]
    with connect() as conn:
        conn.execute(
            "INSERT INTO tenants (tenant_id, name, tier, status, created_at) VALUES (?,?,?,?,?)",
            (tenant_id, name, tier, "active", _now()),
        )
    return tenant_id


def get_tenant(tenant_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM tenants WHERE tenant_id = ?", (tenant_id,)).fetchone()
    return dict(row) if row else None


def list_tenants() -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM tenants ORDER BY created_at").fetchall()
    return [dict(r) for r in rows]


# --- api keys --------------------------------------------------------------

def issue_key(role: str, tenant_id: str | None = None, label: str | None = None) -> tuple[str, str]:
    """Create a key. Returns (key_id, raw_key). The raw key is shown exactly
    once — only its sha256 is stored. Client keys must be tenant-bound; staff
    keys (trainer/admin) must not be."""
    if role == "client":
        if not tenant_id or not get_tenant(tenant_id):
            raise ValueError("client keys require an existing tenant_id")
    elif role in ("trainer", "admin"):
        if tenant_id is not None:
            raise ValueError(f"{role} keys must not be tenant-bound")
    else:
        raise ValueError(f"unknown role: {role}")

    raw_key = f"pk_{role}_" + secrets.token_urlsafe(32)
    key_id = "k_" + uuid.uuid4().hex[:12]
    with connect() as conn:
        conn.execute(
            "INSERT INTO api_keys (key_id, key_hash, tenant_id, role, label, created_at) VALUES (?,?,?,?,?,?)",
            (key_id, hash_key(raw_key), tenant_id, role, label, _now()),
        )
    return key_id, raw_key


def resolve_key(raw_key: str) -> dict | None:
    """Raw key -> {key_id, tenant_id, role} or None. Fail-closed: revoked keys
    and keys of suspended tenants resolve to None."""
    if not raw_key:
        return None
    with connect() as conn:
        row = conn.execute(
            "SELECT key_id, tenant_id, role FROM api_keys WHERE key_hash = ? AND revoked_at IS NULL",
            (hash_key(raw_key),),
        ).fetchone()
        if not row:
            return None
        if row["tenant_id"] is not None:
            tenant = conn.execute(
                "SELECT status FROM tenants WHERE tenant_id = ?", (row["tenant_id"],)
            ).fetchone()
            if not tenant or tenant["status"] != "active":
                return None
    return dict(row)


def revoke_key(key_id: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE api_keys SET revoked_at = ? WHERE key_id = ?", (_now(), key_id))


# --- usage metering --------------------------------------------------------

def record_usage(tenant_id: str, kind: str, tokens_in: int = 0, tokens_out: int = 0,
                 model: str | None = None, est_cost_usd: float = 0.0) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO usage_events (tenant_id, kind, tokens_in, tokens_out, model, est_cost_usd, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (tenant_id, kind, tokens_in, tokens_out, model, est_cost_usd, _now()),
        )


def analyses_this_month(tenant_id: str) -> int:
    month_prefix = datetime.now(timezone.utc).strftime("%Y-%m")
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM usage_events WHERE tenant_id = ? AND kind = 'analysis'"
            " AND created_at LIKE ?",
            (tenant_id, month_prefix + "%"),
        ).fetchone()
    return row["n"]


def tenant_over_limit(tenant_id: str) -> bool:
    tenant = get_tenant(tenant_id)
    if not tenant:
        return True  # unknown tenant: fail closed
    limit = config.TIERS.get(tenant["tier"], {}).get("analyses_per_month", 0)
    return analyses_this_month(tenant_id) >= limit


# --- audit (append-only: no update/delete accessor exists) -----------------

def audit(actor: str, action: str, tenant_id: str | None = None,
          resource: str | None = None, detail: dict | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO audit_log (tenant_id, actor, action, resource, detail, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (tenant_id, actor, action, resource,
             json.dumps(detail, sort_keys=True) if detail else None, _now()),
        )
