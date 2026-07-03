"""tools/ventures/autonomy.py — graduated-autonomy governance resolver.

The rule: trust is *earned by fitness*, and high-stakes always escalates.

  * human_gate (A) — a venture/action consuming a big fraction of the treasury,
    OR anything CRITICAL_GATE (real-money withdrawal, key change, or a spend
    above the 5/call·50/day wallet caps). A human approves.
  * autonomous (C) — a *proven* loop (a venture-kind whose history cleared the
    proven threshold): the swarm launches/deploys/kills within budget.
  * dev_hands (B) — everything else: the swarm acts through its execution hands
    but bounded by the wallet caps, so B can only ever run cheap experiments —
    exactly right for r-selection.

Pure and deterministic (no state mutation, no I/O) so it is fully testable and
composes with the wallet layer, which independently fail-closes above the caps.
"""

from __future__ import annotations

from .genome import DEFAULT_PORTFOLIO_CONFIG, PortfolioConfig

HUMAN_GATE = "human_gate"
DEV_HANDS = "dev_hands"
AUTONOMOUS = "autonomous"

# Actions that are ALWAYS human-gated regardless of cost or proven-ness — the
# CLAUDE.md hard constraints. No mode or proven score bypasses these.
CRITICAL_GATE_ACTIONS = frozenset({
    "wallet_withdrawal",
    "key_change",
    "contract_signing",
    "legal_wrapper",
})


def resolve_autonomy(
    *,
    action: str,
    cost_usd: float,
    treasury_usd: float,
    proven_score: float,
    per_call_cap_usd: float = 5.0,
    per_day_cap_usd: float = 50.0,
    spent_today_usd: float = 0.0,
    config: PortfolioConfig = DEFAULT_PORTFOLIO_CONFIG,
) -> dict:
    """Resolve how a venture action may execute. Returns ``{mode, reason}``.

    Precedence (highest first): CRITICAL_GATE / cap breach -> human_gate; big
    resource fraction -> human_gate; proven loop -> autonomous; else dev_hands.
    """

    # 1. CRITICAL_GATE + wallet-cap overlay — non-negotiable, wins over all.
    if action in CRITICAL_GATE_ACTIONS:
        return {"mode": HUMAN_GATE, "reason": f"critical_gate:{action}"}
    if cost_usd > per_call_cap_usd:
        return {"mode": HUMAN_GATE, "reason": "over_per_call_cap"}
    if spent_today_usd + cost_usd > per_day_cap_usd:
        return {"mode": HUMAN_GATE, "reason": "over_daily_cap"}

    # 2. Big fraction of the treasury -> a human decides (the user's rule A).
    if treasury_usd > 0 and cost_usd / treasury_usd >= config.big_resource_fraction:
        return {"mode": HUMAN_GATE, "reason": "big_resource_fraction"}
    if treasury_usd <= 0 and cost_usd > 0:
        # No treasury to measure against but a real spend -> escalate.
        return {"mode": HUMAN_GATE, "reason": "no_treasury_basis"}

    # 3. Proven loop earns full autonomy (the user's rule C).
    if proven_score >= config.proven_threshold:
        return {"mode": AUTONOMOUS, "reason": "proven_loop"}

    # 4. Everything else runs through dev-hands, capped (rule B).
    return {"mode": DEV_HANDS, "reason": "default_bounded"}


def proven_score(state: dict, kind: str) -> float:
    """Fitness-earned trust for a venture-kind, from its recorded outcomes.

    Each venture that survived apoptosis AND contributed positive revenue adds
    to its kind's score; kills subtract. Tracked in
    ``operational_flags['venture_proofs'][kind]`` as ``{wins, losses}``.
    """

    proofs = state["operational_flags"].get("venture_proofs", {}).get(kind, {})
    return float(proofs.get("wins", 0)) - 0.5 * float(proofs.get("losses", 0))


def record_outcome(state: dict, kind: str, *, win: bool) -> None:
    """Update a venture-kind's proven record (a win graduates it toward C)."""

    proofs = state["operational_flags"].setdefault("venture_proofs", {})
    entry = proofs.setdefault(kind, {"wins": 0, "losses": 0})
    entry["wins" if win else "losses"] += 1
