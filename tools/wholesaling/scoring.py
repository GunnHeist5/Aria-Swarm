"""tools/wholesaling/scoring.py — deterministic distress-lead scoring (0-30).

Sums the configured distress-signal weights for a lead. Pure arithmetic — no LLM
call — so a lead scores identically every run (the reliability fix vs. an
LLM-scored pipeline). Lead dicts are normalized: each signal is a truthy/falsy
field matching a key in ``config.signal_weights``.
"""

from __future__ import annotations

from .config import DEFAULT_CONFIG, WholesalingConfig


def score_lead(lead: dict, config: WholesalingConfig = DEFAULT_CONFIG) -> dict:
    """Score one lead. Returns ``{score, signals, priority}``.

    ``signals`` is the list of distress signals that fired (for the brief /
    audit trail). ``score`` is clamped to ``[0, config.score_max]``.
    ``priority`` is ``score >= config.priority_threshold``.
    """

    fired = [name for name, weight in config.signal_weights.items() if lead.get(name)]
    raw = sum(config.signal_weights[name] for name in fired)
    score = max(0, min(raw, config.score_max))
    return {
        "score": score,
        "signals": fired,
        "priority": score >= config.priority_threshold,
    }


def rank_leads(leads: list[dict], config: WholesalingConfig = DEFAULT_CONFIG) -> list[dict]:
    """Score every lead and return them sorted by score (desc).

    Each result is the original lead merged with its scoring fields, so the
    daily brief can take the top N directly.
    """

    scored = [{**lead, **score_lead(lead, config)} for lead in leads]
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored
