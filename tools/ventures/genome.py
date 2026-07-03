"""tools/ventures/genome.py — the evolvable venture genome + portfolio config.

Every parameter a venture makes decisions from lives here as data, so the
swarm's evolution layer can mutate it and the metabolic ratio can select the
variants that survive apoptosis and contribute revenue. A ``VentureGenome`` is
an immutable snapshot; evolve via ``.mutate()`` (mirrors ``WholesalingConfig``).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace


def _default_kill_criteria() -> dict:
    # A venture dies (apoptosis) when ANY of these trips. Cheap death is the
    # point — selection needs many failures to work.
    return {
        "no_revenue_by_day": 30,      # zero revenue past this many days -> kill
        "min_validation_signal": 0.15,  # stage signal below this at a gate -> kill
        "max_cac_to_ltv": 1.0,        # customer-acq cost >= lifetime value -> kill
        "max_days_alive": 120,        # hard cap; re-propose as a fresh genome
    }


def _default_stage_budgets() -> list:
    # Staged capital release: never all upfront. Each stage unlocks only when
    # the prior stage's validation gate passes. Cheap first (r-selection).
    return [
        {"stage": "validate", "budget_usd": 5.0, "gate_signal": 0.15},
        {"stage": "mvp", "budget_usd": 25.0, "gate_signal": 0.30},
        {"stage": "scale", "budget_usd": 200.0, "gate_signal": 0.50},
    ]


@dataclass(frozen=True)
class VentureGenome:
    """Genome for one venture hypothesis (immutable; evolve via ``mutate``)."""

    kind: str                              # e.g. "micro_saas", "content_site", "automation_service"
    hypothesis: str = ""                   # the falsifiable bet, one sentence
    seed_cap_usd: float = 5.0              # initial cheap-bet capital (r-selection)
    stage_budgets: list = field(default_factory=_default_stage_budgets)
    kill_criteria: dict = field(default_factory=_default_kill_criteria)
    parent_ids: tuple = ()                 # lineage (recombination records both)

    def mutate(self, **changes) -> "VentureGenome":
        """Return a new genome with overrides applied (the mutation operator)."""

        return replace(self, **changes)

    def total_budget_usd(self) -> float:
        """Full capital this genome could consume across all stages."""

        return round(sum(s["budget_usd"] for s in self.stage_budgets), 6)


@dataclass(frozen=True)
class PortfolioConfig:
    """Genome for portfolio behavior — the r/K reproductive strategy."""

    max_concurrent: int = 5            # how many live bets at once
    max_per_kind: int = 2              # cap live bets of one kind (niche diversity)
    big_resource_fraction: float = 0.20  # cost/treasury >= this -> human gate (A)
    proven_threshold: float = 3.0      # proven_score >= this -> full autonomy (C)
    r_bet_cap_usd: float = 5.0         # a "cheap experiment" ceiling
    k_scale_fraction: float = 0.30     # treasury fraction to pour into a winner

    def mutate(self, **changes) -> "PortfolioConfig":
        return replace(self, **changes)


DEFAULT_PORTFOLIO_CONFIG = PortfolioConfig()
