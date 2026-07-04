"""registry.py — the Plasmid Registry: the swarm's evolutionary memory banks.

A zero-dependency ``sqlite3`` storage layer at ``~/.automaton/swarm_genome.db``
implementing the Horizontal Gene Transfer "Shared Plasmid Database" from
``CLAUDE.md``. It is the **data contract** every evolutionary component agrees
on: prompts (`agents/*.md`) are genome — here they become immutable, hashed,
lineage-tracked *plasmids* with fitness scores attached.

Pure storage by design: this module imports no `state`/`graph` runtime; it only
reads the raw `agents/*.md` text (via ``prompts.AGENTS_DIR``) to seed the genesis
genome. The Red Queen sandbox pulls baselines and writes fitness scores here; the
execution harness reads the elite genome back out for hot-swap.

Schema (3 tables):
  swarms          — swarm instances + lineage (parent, generation).
  plasmids        — hashed prompt variations + lineage + production flag.
  evaluation_logs — fitness scores per plasmid (written by the sandbox).
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from prompts import AGENTS_DIR

# ---------------------------------------------------------------------------
# Location & constants
# ---------------------------------------------------------------------------

STATE_DIR = Path("~/.automaton").expanduser()
DB_PATH = STATE_DIR / "swarm_genome.db"

# The cross-swarm Shared Plasmid Database (HGT). Distinct from each swarm's local
# genome DB: every swarm instance broadcasts elite genes here and pulls superior
# foreign genes from here. In a multi-container deploy point this at a shared
# volume / network mount via SWARM_PLASMID_POOL.
SHARED_POOL_PATH = Path(
    os.environ.get("SWARM_PLASMID_POOL", STATE_DIR / "shared_plasmid_pool.db")
)

# Roles whose prompts live as genome files in agents/.
AGENT_ROLES = ("visionary", "realist", "synthesizer", "qualifier")

# Mutation provenance for a plasmid.
MUTATION_GENESIS = "genesis"
MUTATION_POINT = "point"
MUTATION_CROSSOVER = "crossover"
MUTATION_HGT = "hgt"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def plasmid_hash(role: str, prompt_text: str) -> str:
    """Role-scoped SHA-256 of a gene.

    Role-scoping makes identical text under different roles distinct genes, while
    identical (role, text) dedupes across swarms — the basis for HGT sharing.
    """

    digest = hashlib.sha256(f"{role}\x00{prompt_text}".encode("utf-8"))
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


def _connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    """Open a short-lived WAL connection.

    WAL + a generous busy timeout lets the async sandbox writer and the main-loop
    reader operate concurrently without lock contention. Callers open/close per
    operation so no connection holds a write lock across cycles.
    """

    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS swarms (
    swarm_id          TEXT PRIMARY KEY,
    parent_swarm_id   TEXT,
    generation        INTEGER NOT NULL DEFAULT 0,
    genome_hash       TEXT NOT NULL DEFAULT '',
    status            TEXT NOT NULL DEFAULT 'active',
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plasmids (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    plasmid_hash           TEXT NOT NULL UNIQUE,
    role                   TEXT NOT NULL,
    prompt_text            TEXT NOT NULL,
    parent_id              INTEGER REFERENCES plasmids(id),
    parent_plasmid_hash    TEXT,
    generation_id          INTEGER NOT NULL DEFAULT 0,
    mutation_type          TEXT NOT NULL DEFAULT 'point',
    swarm_id               TEXT,
    cleared_for_production INTEGER NOT NULL DEFAULT 0,
    created_at             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_plasmids_role ON plasmids(role);
CREATE INDEX IF NOT EXISTS idx_plasmids_cleared ON plasmids(cleared_for_production);

CREATE TABLE IF NOT EXISTS evaluation_logs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    plasmid_id          INTEGER NOT NULL REFERENCES plasmids(id),
    plasmid_hash        TEXT NOT NULL,
    swarm_id            TEXT,
    fitness_score       REAL NOT NULL,
    metabolic_delta     REAL,
    adversarial_survived INTEGER,
    notes               TEXT,
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_eval_plasmid ON evaluation_logs(plasmid_id);
"""


def initialize_registry(db_path: Path = DB_PATH) -> Path:
    """Create the database and tables if absent. Idempotent."""

    db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    conn = _connect(db_path)
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()
    return db_path


# ---------------------------------------------------------------------------
# Swarms
# ---------------------------------------------------------------------------


def register_swarm(
    swarm_id: str,
    parent_swarm_id: str | None = None,
    generation: int = 0,
    genome_hash: str = "",
    db_path: Path = DB_PATH,
) -> None:
    """Record a swarm instance and its lineage (idempotent on swarm_id)."""

    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO swarms "
            "(swarm_id, parent_swarm_id, generation, genome_hash, status, created_at) "
            "VALUES (?, ?, ?, ?, 'active', ?)",
            (swarm_id, parent_swarm_id, generation, genome_hash, _now()),
        )
        conn.commit()
    finally:
        conn.close()


def set_swarm_status(swarm_id: str, status: str, db_path: Path = DB_PATH) -> None:
    """Update a swarm's lifecycle status (e.g. mark it ``extinct``)."""

    conn = _connect(db_path)
    try:
        conn.execute(
            "UPDATE swarms SET status = ? WHERE swarm_id = ?", (status, swarm_id)
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Plasmids
# ---------------------------------------------------------------------------


def register_plasmid(
    role: str,
    prompt_text: str,
    parent_id: int | None = None,
    *,
    swarm_id: str | None = None,
    mutation_type: str = MUTATION_POINT,
    generation_id: int = 0,
    cleared_for_production: bool = False,
    db_path: Path = DB_PATH,
) -> dict:
    """Store a prompt variation as a hashed, lineage-tracked plasmid.

    Identical (role, prompt_text) dedupes on the unique hash — a repeat call
    returns the existing row with ``deduped=True`` and never forks lineage.
    BUT a dedupe that requests ``cleared_for_production=True`` promotes the
    existing row: otherwise an HGT pull or a learning-adoption of a gene the
    swarm already holds (uncleared) would be a silent no-op.

    Returns ``{"id", "plasmid_hash", "deduped"}``.
    """

    p_hash = plasmid_hash(role, prompt_text)
    conn = _connect(db_path)
    try:
        existing = conn.execute(
            "SELECT id FROM plasmids WHERE plasmid_hash = ?", (p_hash,)
        ).fetchone()
        if existing is not None:
            if cleared_for_production:
                conn.execute(
                    "UPDATE plasmids SET cleared_for_production = 1 WHERE plasmid_hash = ?",
                    (p_hash,),
                )
                conn.commit()
            return {"id": existing["id"], "plasmid_hash": p_hash, "deduped": True,
                    "promoted": bool(cleared_for_production)}

        parent_hash = None
        if parent_id is not None:
            row = conn.execute(
                "SELECT plasmid_hash FROM plasmids WHERE id = ?", (parent_id,)
            ).fetchone()
            parent_hash = row["plasmid_hash"] if row else None

        cur = conn.execute(
            "INSERT INTO plasmids "
            "(plasmid_hash, role, prompt_text, parent_id, parent_plasmid_hash, "
            " generation_id, mutation_type, swarm_id, cleared_for_production, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                p_hash, role, prompt_text, parent_id, parent_hash,
                generation_id, mutation_type, swarm_id,
                1 if cleared_for_production else 0, _now(),
            ),
        )
        conn.commit()
        return {"id": cur.lastrowid, "plasmid_hash": p_hash, "deduped": False}
    finally:
        conn.close()


def clear_for_production(plasmid_hash: str, db_path: Path = DB_PATH) -> None:
    """Promote a plasmid: mark it cleared for production use."""

    conn = _connect(db_path)
    try:
        conn.execute(
            "UPDATE plasmids SET cleared_for_production = 1 WHERE plasmid_hash = ?",
            (plasmid_hash,),
        )
        conn.commit()
    finally:
        conn.close()


def get_plasmid(plasmid_hash: str, db_path: Path = DB_PATH) -> dict | None:
    """Fetch a single plasmid record by hash."""

    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM plasmids WHERE plasmid_hash = ?", (plasmid_hash,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_lineage(plasmid_hash: str, db_path: Path = DB_PATH) -> list[dict]:
    """Walk a plasmid's ancestry from itself to its genesis root.

    Returns ``[self, parent, grandparent, ...]``.
    """

    chain: list[dict] = []
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM plasmids WHERE plasmid_hash = ?", (plasmid_hash,)
        ).fetchone()
        while row is not None:
            chain.append(dict(row))
            parent_id = row["parent_id"]
            if parent_id is None:
                break
            row = conn.execute(
                "SELECT * FROM plasmids WHERE id = ?", (parent_id,)
            ).fetchone()
        return chain
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Evaluations (written by the sandbox)
# ---------------------------------------------------------------------------


def log_evaluation(
    plasmid_hash: str,
    fitness_score: float,
    *,
    swarm_id: str | None = None,
    adversarial_survived: bool | None = None,
    metabolic_delta: float | None = None,
    notes: str | None = None,
    db_path: Path = DB_PATH,
) -> int:
    """Record a fitness score for a plasmid (the sandbox's score-writer).

    Returns the new evaluation row id. Raises ``KeyError`` if the plasmid hash is
    unknown (fail-closed: never log a score against a phantom gene).
    """

    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT id FROM plasmids WHERE plasmid_hash = ?", (plasmid_hash,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown plasmid_hash: {plasmid_hash}")

        survived = None if adversarial_survived is None else int(adversarial_survived)
        cur = conn.execute(
            "INSERT INTO evaluation_logs "
            "(plasmid_id, plasmid_hash, swarm_id, fitness_score, metabolic_delta, "
            " adversarial_survived, notes, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["id"], plasmid_hash, swarm_id, fitness_score,
                metabolic_delta, survived, notes, _now(),
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Active genome selection (read by the harness / sandbox)
# ---------------------------------------------------------------------------

# Per-role, highest mean fitness among production-cleared plasmids. Genesis seeds
# (cleared, not yet scored) fall back to 0.0 so they serve as the baseline until
# something beats them. Optionally scoped to a swarm (else global pool).
_ACTIVE_QUERY = """
SELECT p.role, p.prompt_text, p.plasmid_hash, p.generation_id,
       COALESCE(AVG(e.fitness_score), 0.0) AS fitness
FROM plasmids p
LEFT JOIN evaluation_logs e ON e.plasmid_id = p.id
WHERE p.cleared_for_production = 1
  AND (:swarm_id IS NULL OR p.swarm_id = :swarm_id)
GROUP BY p.id
ORDER BY p.role ASC, fitness DESC, p.created_at DESC
"""


def get_active_plasmids(
    swarm_id: str | None = None, db_path: Path = DB_PATH
) -> dict[str, dict]:
    """Full elite record per role (highest-scoring, production-cleared).

    Returns ``{role: {"prompt_text", "plasmid_hash", "fitness", "generation_id"}}``.
    Reused by the sandbox (lineage/scoring) and by ``get_active_genome``.
    """

    conn = _connect(db_path)
    try:
        rows = conn.execute(_ACTIVE_QUERY, {"swarm_id": swarm_id}).fetchall()
    finally:
        conn.close()

    # Rows are ordered so the first per role is the winner; keep only the first.
    genome: dict[str, dict] = {}
    for row in rows:
        if row["role"] in genome:
            continue
        genome[row["role"]] = {
            "prompt_text": row["prompt_text"],
            "plasmid_hash": row["plasmid_hash"],
            "fitness": row["fitness"],
            "generation_id": row["generation_id"],
        }
    return genome


def get_active_genome(
    swarm_id: str | None = None, db_path: Path = DB_PATH
) -> dict[str, str]:
    """The production contract: ``{role: prompt_text}`` for hot-swap.

    The highest-scoring production-cleared plasmid per role.
    """

    return {
        role: rec["prompt_text"]
        for role, rec in get_active_plasmids(swarm_id, db_path).items()
    }


# ---------------------------------------------------------------------------
# Genesis seeding
# ---------------------------------------------------------------------------


def seed_genesis_genome(swarm_id: str, db_path: Path = DB_PATH) -> dict:
    """Register the baseline `agents/*.md` prompts as generation-0 plasmids.

    Reads the raw template text (no context injection — genome, not a rendered
    prompt), registers each role cleared for production, and records the swarm.
    Gives ``get_active_genome`` an immediate baseline and the sandbox its pull
    source. Idempotent via plasmid hashing.

    Returns ``{role: plasmid_hash}``.
    """

    initialize_registry(db_path)
    register_swarm(swarm_id, generation=0, db_path=db_path)

    seeded: dict[str, str] = {}
    for role in AGENT_ROLES:
        path = AGENTS_DIR / f"{role}.md"
        if not path.is_file():
            raise FileNotFoundError(f"genesis genome missing: {path}")
        text = path.read_text(encoding="utf-8")
        result = register_plasmid(
            role, text,
            swarm_id=swarm_id,
            mutation_type=MUTATION_GENESIS,
            generation_id=0,
            cleared_for_production=True,
            db_path=db_path,
        )
        seeded[role] = result["plasmid_hash"]
    return seeded


# ---------------------------------------------------------------------------
# Horizontal Gene Transfer — the cross-swarm Shared Plasmid Database
# ---------------------------------------------------------------------------

_POOL_SCHEMA = """
CREATE TABLE IF NOT EXISTS plasmid_pool (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    gene_hash       TEXT NOT NULL UNIQUE,
    role            TEXT NOT NULL,
    prompt_text     TEXT NOT NULL,
    origin_swarm_id TEXT,
    fitness_score   REAL NOT NULL DEFAULT 0,
    generation_id   INTEGER NOT NULL DEFAULT 0,
    broadcast_count INTEGER NOT NULL DEFAULT 1,
    broadcast_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pool_role ON plasmid_pool(role);
"""


def initialize_pool(pool_path: Path = SHARED_POOL_PATH) -> Path:
    """Create the shared plasmid pool database if absent. Idempotent."""

    pool_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    conn = _connect(pool_path)
    try:
        conn.executescript(_POOL_SCHEMA)
        conn.commit()
    finally:
        conn.close()
    return pool_path


def _local_best_fitness(plasmid_hash: str, db_path: Path) -> float:
    """Highest recorded fitness for a local plasmid (0.0 if unscored)."""

    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT MAX(fitness_score) AS f FROM evaluation_logs WHERE plasmid_hash = ?",
            (plasmid_hash,),
        ).fetchone()
        return row["f"] if row and row["f"] is not None else 0.0
    finally:
        conn.close()


def broadcast_gene(
    swarm_id: str,
    plasmid_hash: str,
    *,
    db_path: Path = DB_PATH,
    pool_path: Path = SHARED_POOL_PATH,
) -> dict:
    """Publish a local elite gene to the shared pool (HGT broadcast).

    Upserts on the gene hash: the pool always keeps the best-known fitness for a
    gene and counts re-broadcasts. Raises ``KeyError`` for an unknown local hash.

    Returns ``{"gene_hash", "role", "fitness", "broadcast_count"}``.
    """

    rec = get_plasmid(plasmid_hash, db_path)
    if rec is None:
        raise KeyError(f"unknown local plasmid_hash: {plasmid_hash}")

    fitness = _local_best_fitness(plasmid_hash, db_path)
    initialize_pool(pool_path)
    conn = _connect(pool_path)
    try:
        conn.execute(
            "INSERT INTO plasmid_pool "
            "(gene_hash, role, prompt_text, origin_swarm_id, fitness_score, "
            " generation_id, broadcast_count, broadcast_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, ?) "
            "ON CONFLICT(gene_hash) DO UPDATE SET "
            "  fitness_score = MAX(fitness_score, excluded.fitness_score), "
            "  broadcast_count = broadcast_count + 1, "
            "  broadcast_at = excluded.broadcast_at",
            (rec["plasmid_hash"], rec["role"], rec["prompt_text"], swarm_id,
             fitness, rec["generation_id"], _now()),
        )
        conn.commit()
        row = conn.execute(
            "SELECT broadcast_count FROM plasmid_pool WHERE gene_hash = ?",
            (plasmid_hash,),
        ).fetchone()
    finally:
        conn.close()
    return {
        "gene_hash": plasmid_hash, "role": rec["role"],
        "fitness": fitness, "broadcast_count": row["broadcast_count"],
    }


def broadcast_active_genome(
    swarm_id: str,
    *,
    db_path: Path = DB_PATH,
    pool_path: Path = SHARED_POOL_PATH,
) -> list[dict]:
    """Broadcast this swarm's *proven* production champions (fitness > 0).

    Baseline genes (no evaluations yet, fitness 0) are not shared — only genes
    the swarm has actually validated propagate into the pool.
    """

    shared = []
    for role, rec in get_active_plasmids(swarm_id, db_path).items():
        if rec["fitness"] > 0:
            shared.append(broadcast_gene(
                swarm_id, rec["plasmid_hash"], db_path=db_path, pool_path=pool_path
            ))
    return shared


def pull_elite_genes(
    swarm_id: str,
    roles=AGENT_ROLES,
    *,
    db_path: Path = DB_PATH,
    pool_path: Path = SHARED_POOL_PATH,
    min_advantage: float = 0.0,
) -> list[dict]:
    """Pull superior foreign genes from the pool and hot-swap them in (HGT pull).

    For each role, adopt the highest-fitness pool gene that (a) did not originate
    from this swarm and (b) beats this swarm's local production champion by more
    than ``min_advantage``. Adopted genes are registered locally as ``hgt``
    plasmids cleared for production and scored with the pool fitness — so
    ``get_active_genome`` immediately serves them.

    The fitness filter makes this idempotent: once adopted+scored, a gene no
    longer beats the local champion, so re-pulling won't re-adopt it.

    Returns the list of adopted ``{role, gene_hash, origin_swarm_id, fitness}``.
    """

    initialize_registry(db_path)
    initialize_pool(pool_path)
    local = get_active_plasmids(swarm_id, db_path)

    adopted: list[dict] = []
    conn = _connect(pool_path)
    try:
        for role in roles:
            local_fit = local.get(role, {}).get("fitness", -1.0)
            cand = conn.execute(
                "SELECT * FROM plasmid_pool "
                "WHERE role = ? AND (origin_swarm_id IS NULL OR origin_swarm_id != ?) "
                "ORDER BY fitness_score DESC LIMIT 1",
                (role, swarm_id),
            ).fetchone()
            if cand is None or cand["fitness_score"] <= local_fit + min_advantage:
                continue

            register_plasmid(
                role, cand["prompt_text"], swarm_id=swarm_id,
                mutation_type=MUTATION_HGT, generation_id=cand["generation_id"],
                cleared_for_production=True, db_path=db_path,
            )
            clear_for_production(cand["gene_hash"], db_path)
            log_evaluation(
                cand["gene_hash"], cand["fitness_score"], swarm_id=swarm_id,
                notes=f"hgt pull from {cand['origin_swarm_id']}", db_path=db_path,
            )
            adopted.append({
                "role": role, "gene_hash": cand["gene_hash"],
                "origin_swarm_id": cand["origin_swarm_id"],
                "fitness": cand["fitness_score"],
            })
    finally:
        conn.close()
    return adopted


def list_pool(role: str | None = None, pool_path: Path = SHARED_POOL_PATH) -> list[dict]:
    """Inspect the shared pool (optionally filtered to a role)."""

    initialize_pool(pool_path)
    conn = _connect(pool_path)
    try:
        if role is None:
            rows = conn.execute(
                "SELECT * FROM plasmid_pool ORDER BY role, fitness_score DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM plasmid_pool WHERE role = ? ORDER BY fitness_score DESC",
                (role,),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
