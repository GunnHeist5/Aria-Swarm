"""tools/ventures/recombine.py — sexual recombination of venture genomes.

Cloning propagates a winner unchanged; recombination *blends* two winners into
a hybrid that may beat both. This is the venture-blueprint analogue of
``sandbox.crossover_prompts`` (which recombines agent prose genes): here we mix
two ``VentureGenome`` snapshots field-by-field into a child, recording both
parents in the lineage.
"""

from __future__ import annotations

from .genome import VentureGenome


def recombine(
    genome_a: VentureGenome,
    genome_b: VentureGenome,
    *,
    new_kind: str | None = None,
) -> VentureGenome:
    """Blend two winning venture genomes into a hybrid child.

    Numeric fields average (blend the working parameters); the cheaper seed is
    inherited (stay r-selected); ``kill_criteria`` take the stricter bound of
    each parent (a hybrid should die at least as fast as its stricter parent);
    stages come from parent A; lineage records both parents. Deterministic — no
    randomness (evolution's variety comes from *which* pairs are recombined).
    """

    def _stricter(key: str, lower_is_stricter: bool) -> float:
        va = genome_a.kill_criteria.get(key)
        vb = genome_b.kill_criteria.get(key)
        vals = [v for v in (va, vb) if v is not None]
        if not vals:
            return None
        return min(vals) if lower_is_stricter else max(vals)

    kill = {
        "no_revenue_by_day": _stricter("no_revenue_by_day", lower_is_stricter=True),
        "min_validation_signal": _stricter("min_validation_signal", lower_is_stricter=False),
        "max_cac_to_ltv": _stricter("max_cac_to_ltv", lower_is_stricter=True),
        "max_days_alive": _stricter("max_days_alive", lower_is_stricter=True),
    }
    kill = {k: v for k, v in kill.items() if v is not None}

    hypothesis = f"hybrid: {genome_a.hypothesis} × {genome_b.hypothesis}".strip(" ×")
    return VentureGenome(
        kind=new_kind or genome_a.kind,
        hypothesis=hypothesis,
        seed_cap_usd=min(genome_a.seed_cap_usd, genome_b.seed_cap_usd),
        stage_budgets=[dict(s) for s in genome_a.stage_budgets],
        kill_criteria=kill,
        parent_ids=(genome_a.kind, genome_b.kind),
    )
