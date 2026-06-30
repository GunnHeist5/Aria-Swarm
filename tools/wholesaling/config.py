"""tools/wholesaling/config.py — the evolvable wholesaling genome.

Every number the operation uses to make a decision lives here, as data — so the
swarm's evolution layer (sandbox mutation + HGT) can tune it and the metabolic
ratio can select the variants that close more deals. Defaults are exactly
Muffin's published parameters.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace


def _default_signal_weights() -> dict:
    # Muffin's distress-signal weights (0-30 scale).
    return {
        "vacant_or_land": 8,
        "foreclosure": 10,
        "distressed": 5,
        "equity": 3,
        "tax_delinquent": 3,
    }


@dataclass(frozen=True)
class WholesalingConfig:
    """Genome parameters for the wholesaling operator.

    Frozen so a config is an immutable genome snapshot; evolve via ``mutate``
    (returns a new config) rather than mutating in place.
    """

    # --- Lead scoring ---
    signal_weights: dict = field(default_factory=_default_signal_weights)
    score_max: int = 30                 # clamp ceiling for the total score
    priority_threshold: int = 20        # score >= this => high-priority lead

    # --- Max-offer formula:  (ARV * arv_multiplier) - (Repair * repair_multiplier) - assignment_fee ---
    arv_multiplier: float = 0.70
    repair_multiplier: float = 1.20
    assignment_fee_usd: float = 15_000.0

    # --- Escalation triggers ---
    close_window_days: tuple = (7, 10)  # standard close window
    repair_cap_usd: float = 150_000.0   # repairs >= this => escalate
    require_cash_at_close: bool = True

    def mutate(self, **changes) -> "WholesalingConfig":
        """Return a new config with overrides applied (the mutation operator)."""

        return replace(self, **changes)


# The baseline genome (Muffin's published parameters).
DEFAULT_CONFIG = WholesalingConfig()
