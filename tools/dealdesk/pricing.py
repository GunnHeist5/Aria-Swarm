"""tools/dealdesk/pricing.py — turn a property record into a negotiation band.

Runs the swarm's own deterministic offer math (``tools/wholesaling/deals``) on
the looked-up property and returns ``{opening_offer, max_offer, escalate}``. The
ceiling (``max_offer``) is the non-negotiable cap the voice agent must never
exceed; ``opening_offer`` is the anchor it starts from, leaving negotiation room.

Everything that shouldn't be auto-priced fails closed to ``escalate`` (with a
reason): no valuation, listed with an agent, encumbered beyond the offer, not
land, or an offer below the minimum worth pursuing.

The pricing PARAMETERS are genome (``WholesalingConfig``) and env-overridable —
**land needs its own tuning** (the default $15k assignment fee is a house
number; cheap infill lots want a much smaller fee or every deal escalates).
"""

from __future__ import annotations

import os

from tools.wholesaling.config import DEFAULT_CONFIG
from tools.wholesaling.deals import evaluate_deal, max_offer

from .lookup import PropertyRecord

# Land-oriented, env-overridable knobs. Defaults keep the swarm's strategy but
# expose the ones that MUST be tuned for land.
OPENING_FRACTION = float(os.environ.get("DEALDESK_OPENING_FRACTION", "0.85"))
REPAIR_DEFAULT_USD = float(os.environ.get("DEALDESK_REPAIR_DEFAULT_USD", "0"))  # land ~ 0
MIN_VIABLE_OFFER_USD = float(os.environ.get("DEALDESK_MIN_VIABLE_OFFER_USD", "1000"))

_LISTED = {"active", "pending", "contingent"}


def _config():
    """Build the pricing config from the swarm genome + deal-desk env overrides."""

    changes = {}
    arv_mult = os.environ.get("DEALDESK_ARV_MULTIPLIER")
    fee = os.environ.get("DEALDESK_ASSIGNMENT_FEE_USD")
    if arv_mult:
        changes["arv_multiplier"] = float(arv_mult)
    if fee:
        changes["assignment_fee_usd"] = float(fee)
    return DEFAULT_CONFIG.mutate(**changes) if changes else DEFAULT_CONFIG


def _round100(x: float) -> float:
    return float(round(x / 100.0) * 100)


def _escalate(reason: str, record: PropertyRecord | None = None) -> dict:
    return {
        "found": record is not None,
        "opening_offer": None,
        "max_offer": None,
        "escalate": True,
        "escalate_reason": reason,
        "property": _prop(record),
        "notes": f"pricing escalated: {reason}",
    }


def _prop(record: PropertyRecord | None) -> dict | None:
    if record is None:
        return None
    return {
        "address": record.address, "city": record.city,
        "county": record.county, "state": record.state, "apn": record.apn,
    }


def compute_offer_range(record: PropertyRecord | None) -> dict:
    """Return the negotiation band for a property, or an escalate flag.

    Never exposes the ARV / formula / margin in the response — only the band —
    so the voice agent cannot leak how offers are computed.
    """

    if record is None:
        return _escalate("not_found")

    # Land only: the strategy is for vacant land; anything with a structure/type
    # that isn't land goes to a human.
    ptype = (record.property_type or "").lower()
    if ptype and "land" not in ptype and "vacant" not in ptype and "lot" not in ptype:
        return _escalate("not_land", record)

    # Listed with an agent -> a human/agent is involved; don't cold-price it.
    if (record.mls_status or "").lower() in _LISTED:
        return _escalate("listed_with_agent", record)

    arv = record.est_value or record.assessed_value
    if not arv or arv <= 0:
        return _escalate("no_valuation", record)

    config = _config()
    ceiling = max_offer(arv, REPAIR_DEFAULT_USD, config)

    # Encumbrance: if what's owed (loans + liens) meets or exceeds our ceiling,
    # a simple cash purchase can't clear title — escalate (short-sale territory).
    owed = (record.open_loans_balance or 0.0) + (record.lien_amount or 0.0)
    if owed and owed >= ceiling:
        return _escalate("encumbered", record)

    if ceiling < MIN_VIABLE_OFFER_USD:
        # Formula yields nothing worth pursuing (often the flat fee vs. a cheap
        # lot — a signal the land config needs tuning).
        return _escalate("below_min_viable", record)

    # Surface any deal-structure escalations the shared rules flag (title, etc.).
    evald = evaluate_deal(arv, REPAIR_DEFAULT_USD, config=config)
    opening = _round100(min(ceiling, ceiling * OPENING_FRACTION))
    ceiling = _round100(ceiling)
    return {
        "found": True,
        "opening_offer": opening,
        "max_offer": ceiling,
        "escalate": False,
        "escalate_reason": None,
        "property": _prop(record),
        "notes": (f"structure_flags:{evald['escalations']}" if evald["escalations"]
                  else "clean"),
    }
