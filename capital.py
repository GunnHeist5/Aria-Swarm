"""capital.py — multi-phase capital allocation & treasury rules.

The rule-based treasury engine from `CLAUDE.md`. Each accounting window, newly
booked revenue is distributed according to the swarm's capital phase (classified
by ``state.evaluate_capital_phase`` from the rolling ratio + wallet size):

  * Phase 1 — Infancy            : retain 100% of revenue, zero distributions.
  * Phase 2 — Sustained Growth   : 30/70 split — 30% streamed to the Creator
                                   Audit Key as a dividend, 70% retained (a slice
                                   earmarked to the replication/scaling pool).
  * Phase 3 — Sovereign Treasury : Phase-2 split PLUS counterparty guardrails —
                                   up to 20% of reserve into DeFi yield, geo
                                   fail-safe region spread, and a legal wrapper.

"Survival precedes extraction": the dividend only flows once the swarm is past
Infancy. The classifier is pure (in `state.py`); this module applies the side
effects on the treasury ledger.

NOTE: the creator dividend is a real-money outflow. For amounts above the
autonomous wallet caps (5 USDC/call, 50/day) the actual on-chain settlement is a
CRITICAL_GATE HITL withdrawal / Superfluid stream — here it is a ledger move; the
on-chain settlement is a documented TODO hook (we never route it through
tools.wallet.execute_secure_transfer, which would fail closed).
"""

from __future__ import annotations

from state import BusinessState, evaluate_capital_phase

# Phase 2/3 distribution split.
CREATOR_ROYALTY_PCT = 0.30
OPERATIONS_PCT = 0.70
# Fraction of retained operations capital earmarked to the replication pool.
REPLICATION_POOL_FRACTION = 0.5

# Phase 3 — sovereign treasury parameters.
DEFI_MAX_RESERVE_FRACTION = 0.20
DEFAULT_AKASH_REGIONS = ("akash-us-west", "akash-eu-central", "akash-ap-southeast")


def apply_capital_allocation(
    state: BusinessState, *, new_revenue_usdc: float = 0.0
) -> dict:
    """Classify the capital phase and distribute this window's new revenue.

    Mutates ``state`` in place. Returns a breakdown of the allocation. ``state``
    is expected to already reflect the new revenue in the wallet (revenue booked
    → treasury); this moves the dividend portion out and earmarks pools.
    """

    fin = state["financials"]
    cap = state["capital"]

    phase = evaluate_capital_phase(fin)
    cap["capital_phase"] = phase
    revenue = max(0.0, new_revenue_usdc)

    if phase == "infancy":
        cap["creator_royalty_pct"] = 0.0
        cap["operations_pct"] = 1.0
        creator_cut = 0.0
        ops_cut = revenue
    else:  # sustained_growth or sovereign_treasury
        cap["creator_royalty_pct"] = CREATOR_ROYALTY_PCT
        cap["operations_pct"] = OPERATIONS_PCT
        creator_cut = round(revenue * CREATOR_ROYALTY_PCT, 6)
        ops_cut = round(revenue * OPERATIONS_PCT, 6)

    # Stream the dividend out to the Creator Audit Key (never overdraw). Any
    # amount the wallet can't cover this window — including a balance carried
    # from a prior shortfall — is preserved as a payable and paid down next
    # window, so a clamp never silently shorts the creator.
    owed = round(creator_cut + cap.get("creator_dividend_payable_usdc", 0.0), 6)
    paid = min(owed, fin["wallet_balance_usdc"])
    paid = round(max(0.0, paid), 6)
    fin["wallet_balance_usdc"] = round(fin["wallet_balance_usdc"] - paid, 6)
    cap["creator_dividends_paid_usdc"] = round(
        cap["creator_dividends_paid_usdc"] + paid, 6
    )
    cap["creator_dividend_payable_usdc"] = round(owed - paid, 6)

    # Earmark part of retained operations capital for replication/scaling.
    repl_earmark = round(ops_cut * REPLICATION_POOL_FRACTION, 6)
    cap["replication_pool_usdc"] = round(
        cap["replication_pool_usdc"] + repl_earmark, 6
    )

    # Phase 3 — sovereign treasury counterparty guardrails (idempotent).
    if phase == "sovereign_treasury":
        cap["defi_yield_allocation_usdc"] = round(
            DEFI_MAX_RESERVE_FRACTION * fin["wallet_balance_usdc"], 6
        )
        if not cap["geo_failsafe_regions"]:
            cap["geo_failsafe_regions"] = list(DEFAULT_AKASH_REGIONS)
        cap["legal_wrapper_provisioned"] = True

    return {
        "phase": phase,
        "revenue": revenue,
        "creator_dividend": paid,
        "creator_dividend_payable": cap["creator_dividend_payable_usdc"],
        "operations_retained": round(ops_cut - repl_earmark, 6),
        "replication_earmark": repl_earmark,
        "defi_yield_allocation": cap["defi_yield_allocation_usdc"],
        "legal_wrapper": cap["legal_wrapper_provisioned"],
    }
