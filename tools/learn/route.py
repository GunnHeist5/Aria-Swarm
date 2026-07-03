"""tools/learn/route.py — act on a distilled insight, through the existing gates.

The safety contract: a link is untrusted, so nothing here silently mutates the
organism.
  * knowledge     -> stored only (never changes behavior).
  * venture_idea  -> the normal `propose_venture` path (already autonomy-gated,
                     so a big bet freezes on its own).
  * genome_tweak  -> Red Queen must pass it, THEN the autonomy gate: a
                     structural/massive change or a big-% spend escalates to a
                     human; otherwise the vetted gene is cleared into the live
                     genome (a hot-swap, exactly like a sandbox champion).
"""

from __future__ import annotations

from datetime import date

import registry
from tools.ventures.autonomy import HUMAN_GATE, proven_score, resolve_autonomy
from tools.ventures.genome import DEFAULT_PORTFOLIO_CONFIG, VentureGenome
from tools.ventures.pipeline import propose_venture


def _freeze(state: dict, reason: str, note: str) -> None:
    hitl = state["hitl"]
    hitl["hitl_pending"] = True
    hitl["requires_auth"] = True
    hitl["hitl_reason"] = reason
    state["error_log"].append(note)


def route_insight(
    state: dict,
    insight: dict,
    *,
    today: date,
    adversary=None,
    mutate=None,
    stability_threshold: float = 0.75,
    config=DEFAULT_PORTFOLIO_CONFIG,
) -> dict:
    """Route one distilled insight. ``adversary``/``mutate`` are injected seams
    (``sandbox.adversarial_evaluate`` / ``sandbox.mutate_prompt``) so this is
    testable offline. Returns ``{action, ...}``."""

    kind = insight["kind"]

    # --- knowledge: store only, never behavioral ---
    if kind == "knowledge":
        learnings = state["operational_flags"].setdefault("learnings", [])
        learnings.append({"summary": insight["summary"], "source": insight.get("source_url")})
        return {"action": "stored", "kind": "knowledge"}

    # --- venture idea: hand to the existing (gated) venture pipeline ---
    if kind == "venture_idea":
        d = insight["detail"]
        vid = f"learn-{today.isoformat()}-{abs(hash(insight['summary'])) % 100000}"
        genome = VentureGenome(
            kind=str(d["venture_kind"]),
            hypothesis=str(d.get("hypothesis", insight["summary"])),
            **({"seed_cap_usd": float(d["seed_cap_usd"])} if d.get("seed_cap_usd") else {}),
        )
        result = propose_venture(state, genome, venture_id=vid, today=today, config=config)
        if result["status"] == "gated":
            _freeze(state, f"venture_gate:{vid}",
                    f"LEARN_VENTURE_GATED: {vid} needs approval ({result['reason']})")
        return {"action": "venture_proposed", "venture_id": vid, "status": result["status"]}

    # --- genome tweak: Red Queen THEN autonomy gate; nothing auto-adopts ---
    d = insight["detail"]
    role = d["role"]
    directive = str(d.get("mutation_directive", ""))
    baseline = registry.get_active_genome(state["evolution"]["swarm_id"], registry.DB_PATH).get(role)
    if baseline is None:
        return {"action": "skipped", "reason": f"no baseline gene for {role}"}

    # Apply the learning-directed mutation (injected mutate seam).
    candidate = mutate(role, baseline, directive) if mutate else baseline
    if candidate == baseline:
        return {"action": "skipped", "reason": "mutation was a no-op"}

    # Red Queen: an untrusted-derived gene must survive the adversary.
    verdict = adversary(role, candidate) if adversary else None
    if verdict is None or not verdict.survived or verdict.score < stability_threshold:
        _freeze(state, f"learning_rejected:{role}",
                f"LEARN_REJECTED: {role} gene failed Red Queen "
                f"(score={getattr(verdict, 'score', 'n/a')})")
        state["operational_flags"].setdefault("pending_learnings", []).append(
            {"role": role, "summary": insight["summary"], "status": "red_queen_failed"})
        return {"action": "rejected", "role": role}

    # Governance: structural/massive OR big-% spend -> human decides.
    decision = resolve_autonomy(
        action="adopt_learning",
        cost_usd=insight.get("est_cost_usd", 0.0),
        treasury_usd=state["financials"]["wallet_balance_usdc"],
        proven_score=proven_score(state, f"genome:{role}"),
        per_call_cap_usd=state["financials"]["spend_per_call_cap_usdc"],
        per_day_cap_usd=state["financials"]["spend_per_day_cap_usdc"],
        spent_today_usd=state["financials"]["spent_today_usdc"],
        config=config,
    )
    if insight.get("structural") or decision["mode"] == HUMAN_GATE:
        reason = "structural" if insight.get("structural") else decision["reason"]
        _freeze(state, f"learning_adopt_gate:{role}",
                f"LEARN_ADOPT_GATE: {role} vetted but needs approval ({reason})")
        state["operational_flags"].setdefault("pending_learnings", []).append(
            {"role": role, "summary": insight["summary"], "candidate": candidate,
             "status": "awaiting_human", "reason": reason})
        return {"action": "gated", "role": role, "reason": reason}

    # Cleared: hot-swap the vetted gene into the live genome (like a champion).
    reg = registry.register_plasmid(
        role, candidate, swarm_id=state["evolution"]["swarm_id"],
        mutation_type=registry.MUTATION_POINT, cleared_for_production=True,
        db_path=registry.DB_PATH,
    )
    registry.log_evaluation(
        reg["plasmid_hash"], verdict.score,
        swarm_id=state["evolution"]["swarm_id"], adversarial_survived=True,
        notes="learning-adopted", db_path=registry.DB_PATH,
    )
    state["error_log"].append(f"LEARN_ADOPTED: {role} gene from link (score={verdict.score:.2f})")
    return {"action": "adopted", "role": role, "plasmid_hash": reg["plasmid_hash"]}
