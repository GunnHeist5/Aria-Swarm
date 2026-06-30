"""tools/wholesaling/deals.py — max-offer math + escalation triggers.

The non-negotiable offer ceiling and the escalation rules from Muffin. Pure
functions: the operation never computes an offer above the formula, and any
deal that trips a trigger is flagged for human review (the swarm's HITL gate in
a later increment).
"""

from __future__ import annotations

from .config import DEFAULT_CONFIG, WholesalingConfig


def max_offer(arv: float, repair: float, config: WholesalingConfig = DEFAULT_CONFIG) -> float:
    """The hard ceiling: ``(ARV * arv_mult) - (Repair * repair_mult) - assignment_fee``.

    Floored at 0 (a formula that goes negative means "no offer", not a credit).
    Rounded to cents.
    """

    offer = (
        arv * config.arv_multiplier
        - repair * config.repair_multiplier
        - config.assignment_fee_usd
    )
    return round(max(0.0, offer), 2)


def evaluate_deal(
    arv: float,
    repair: float,
    *,
    seller_ask: float | None = None,
    close_days: int | None = None,
    cash_at_close: bool = True,
    title_clear: bool = True,
    environmental_hazard: bool = False,
    config: WholesalingConfig = DEFAULT_CONFIG,
) -> dict:
    """Evaluate a deal against the offer ceiling + escalation triggers.

    Returns ``{max_offer, within_formula, escalations}``. ``escalations`` lists
    every trigger that fired (empty => clean, autonomous-eligible up to the
    offer ceiling; non-empty => route to human review). The returned
    ``max_offer`` is never above the formula.
    """

    ceiling = max_offer(arv, repair, config)
    escalations: list[str] = []

    if seller_ask is not None and seller_ask > ceiling:
        escalations.append("price_above_formula")

    if close_days is not None:
        lo, hi = config.close_window_days
        if not (lo <= close_days <= hi):
            escalations.append("nonstandard_close")

    if config.require_cash_at_close and not cash_at_close:
        escalations.append("financing_not_cash")

    if not title_clear:
        escalations.append("title_or_lien_issue")

    if repair >= config.repair_cap_usd:
        escalations.append("repair_cap")

    if environmental_hazard:
        escalations.append("environmental_hazard")

    return {
        "max_offer": ceiling,
        "within_formula": seller_ask is None or seller_ask <= ceiling,
        "escalations": escalations,
    }
