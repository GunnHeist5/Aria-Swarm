"""evolution.py — metabolic lifecycle transitions: Replication & Extinction.

The two endpoints of `CLAUDE.md`'s metabolic loop:

  * **Extinction** — the wallet is depleted; the swarm self-terminates gracefully
    (marks itself extinct in the registry; the harness persists a final snapshot
    and exits).
  * **Replication** — the wallet crosses the surplus threshold; the parent spawns
    a clone: a new swarm id + lineage, a *mutated* copy of the parent's genome
    (the Mutation Operator), a funded child wallet, and a hydratable child state
    snapshot for an independent container to pick up.

The lifecycle *classifier* (`state.evaluate_metabolic_state`) is pure and lives
in `state.py`; this module performs the *side effects*. It is imported only by
`main.py`, so importing `sandbox`/`registry` here is acyclic.
"""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path

import registry
from sandbox import mutate_prompt
from state import new_business_state

logger = logging.getLogger(__name__)

# Fraction of the parent wallet handed to a new child at replication.
DEFAULT_FUND_FRACTION = 0.5


# ---------------------------------------------------------------------------
# Extinction
# ---------------------------------------------------------------------------


def extinct(values: dict, *, db_path: Path = registry.DB_PATH) -> None:
    """Graceful self-termination: mark the swarm extinct and freeze it.

    Mutates ``values`` in place (the caller persists the final snapshot and exits
    non-zero). Registry status is updated best-effort — a missing registry must
    not prevent the swarm from dying cleanly.
    """

    swarm_id = values["evolution"]["swarm_id"]
    values["metabolic_state"] = "extinction"
    values["frozen"] = True
    values["error_log"].append(
        "EXTINCTION: wallet depleted — graceful self-termination"
    )
    try:
        registry.set_swarm_status(swarm_id, "extinct", db_path)
    except Exception as exc:  # pragma: no cover - registry optional at death
        logger.warning("could not mark %s extinct in registry: %s", swarm_id, exc)


# ---------------------------------------------------------------------------
# Replication
# ---------------------------------------------------------------------------


def replicate(
    parent_values: dict,
    *,
    db_path: Path = registry.DB_PATH,
    pool_path: Path = registry.SHARED_POOL_PATH,
    children_dir: Path,
    fund_fraction: float = DEFAULT_FUND_FRACTION,
    mutate: bool = True,
) -> dict:
    """Spawn a mutated, funded child swarm from a surplus parent.

    Steps: derive a child id + lineage, copy-and-mutate the parent's active
    genome into the registry under the child id (cleared for production so the
    child boots on it), debit the parent wallet to fund the child, record the
    child in the parent's lineage, and write the child's ``BusinessState``
    snapshot to ``children_dir`` for an independent container to hydrate.

    Mutates ``parent_values`` in place (wallet debit, child id, reset to growth).
    Returns a summary dict.
    """

    pev = parent_values["evolution"]
    pfin = parent_values["financials"]
    parent_id = pev["swarm_id"]
    gen = pev["generation"]

    child_id = f"{parent_id}-g{gen + 1}-{uuid.uuid4().hex[:6]}"
    funding = round(pfin["wallet_balance_usdc"] * fund_fraction, 6)

    # --- Build the child state, seeded with its funding ---
    child = new_business_state(
        swarm_id=child_id,
        creator_audit_key=parent_values["capital"]["creator_audit_key"],
        seed_capital_usdc=funding,
        replication_threshold_usdc=pfin["replication_threshold_usdc"],
    )
    child["evolution"]["parent_swarm_id"] = parent_id
    child["evolution"]["generation"] = gen + 1

    # --- Mutate the parent's genome into the child's (the Mutation Operator) ---
    registry.register_swarm(
        child_id, parent_swarm_id=parent_id, generation=gen + 1, db_path=db_path
    )
    parent_genome = registry.get_active_genome(parent_id, db_path)
    mutated_roles: list[str] = []
    for role, text in parent_genome.items():
        new_text = text
        if mutate:
            try:
                new_text = mutate_prompt(role, text)
                if new_text != text:
                    mutated_roles.append(role)
            except Exception as exc:  # inherit unmutated rather than fail to spawn
                logger.warning("mutation failed for %s, inheriting: %s", role, exc)
                new_text = text
        # Stamp the child's lineage into the gene text. This keeps each child's
        # genes uniquely hashed (and therefore child-owned, not deduped onto the
        # parent's plasmid) even when the Mutation Operator inherited the text
        # unchanged. A trailing HTML comment preserves all ## headers and
        # {placeholders}, so the gene stays a valid template.
        child_text = f"{new_text}\n<!-- lineage: child={child_id} parent={parent_id} gen={gen + 1} -->"
        parent_plasmid = registry.get_plasmid(
            registry.plasmid_hash(role, text), db_path
        )
        registry.register_plasmid(
            role, child_text,
            parent_id=parent_plasmid["id"] if parent_plasmid else None,
            swarm_id=child_id,
            mutation_type=registry.MUTATION_POINT,
            generation_id=gen + 1,
            cleared_for_production=True,
            db_path=db_path,
        )

    # --- Fund the child by debiting the parent treasury ---
    # NOTE: a real on-chain parent->child transfer is a CRITICAL_GATE HITL
    # withdrawal (far above the 5/50 autonomous wallet cap) — it must NOT go
    # through tools.wallet.execute_secure_transfer (which would fail closed).
    # Here we move it at the ledger level; on-chain settlement is a TODO hook.
    pfin["wallet_balance_usdc"] = round(pfin["wallet_balance_usdc"] - funding, 6)
    child["operational_flags"]["pending_funding_from"] = parent_id
    child["operational_flags"]["pending_funding_usdc"] = funding

    # --- Record lineage; replication consumed the surplus -> back to growth ---
    pev["child_swarm_ids"] = sorted({*pev.get("child_swarm_ids", []), child_id})
    parent_values["metabolic_state"] = "growth"

    # --- Persist the child snapshot for an independent container to hydrate ---
    # (Actual container/Akash deployment is the documented out-of-scope hook.)
    children_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    snapshot = children_dir / f"{child_id}.json"
    snapshot.write_text(json.dumps(child, indent=2, default=str), encoding="utf-8")

    logger.info(
        "replicated %s -> %s (funded %.2f USDC, mutated %s)",
        parent_id, child_id, funding, mutated_roles,
    )
    return {
        "child_swarm_id": child_id,
        "funded_usdc": funding,
        "mutated_roles": mutated_roles,
        "generation": gen + 1,
        "snapshot": str(snapshot),
    }
