"""tools/revenue.py — book real revenue into the swarm treasury.

The bridge between a closed wholesaling deal and the swarm's economics. When a
deal closes, the swarm's 10% cut arrives as USDC in the operational wallet; this
module records it so the metabolic ratio, capital phase, and lifecycle logic run
on *real* income instead of paper numbers.

Tool-agnostic: a human, a CRM webhook, or a Zapier step can record a deal with
one call / one CLI command. Idempotent per ``deal_id`` so a retried webhook never
double-counts.

  python -m tools.revenue --deal-id D123 --assignment-fee 12000
    -> books 10% ($1,200) of a $12,000 assignment fee into the treasury.

Fiat -> USDC routing (the operational step around this):
  The cut closes in USD (title wire). To get the 10% into the USDC wallet,
  on close you convert + send it to the swarm address (manual via Coinbase now,
  automated via the CDP API later). This CLI records what ARRIVED — run it after
  the USDC lands so the ledger matches the chain.
"""

from __future__ import annotations

import argparse

DEFAULT_SWARM_CUT_PCT = 0.10


def book_closed_deal(
    state: dict,
    *,
    deal_id: str,
    assignment_fee_usd: float,
    swarm_cut_pct: float = DEFAULT_SWARM_CUT_PCT,
) -> dict:
    """Record the swarm's cut of a closed deal into ``state`` (in place).

    Credits both ``revenue_generated_usdc`` (drives the metabolic ratio) and
    ``wallet_balance_usdc`` (the USDC actually arrived). Idempotent per
    ``deal_id`` via ``operational_flags["booked_deals"]``.

    Returns ``{"status": "booked"|"duplicate", "deal_id", "cut_usdc", ...}``.
    """

    if assignment_fee_usd < 0:
        raise ValueError("assignment_fee_usd must be non-negative")
    if not 0.0 < swarm_cut_pct <= 1.0:
        raise ValueError("swarm_cut_pct must be in (0, 1]")

    flags = state["operational_flags"]
    booked = flags.setdefault("booked_deals", {})
    if deal_id in booked:
        return {
            "status": "duplicate", "deal_id": deal_id,
            "cut_usdc": booked[deal_id]["cut_usdc"],
        }

    cut = round(assignment_fee_usd * swarm_cut_pct, 6)
    fin = state["financials"]
    fin["revenue_generated_usdc"] = round(fin["revenue_generated_usdc"] + cut, 6)
    fin["wallet_balance_usdc"] = round(fin["wallet_balance_usdc"] + cut, 6)

    booked[deal_id] = {
        "assignment_fee_usd": assignment_fee_usd,
        "swarm_cut_pct": swarm_cut_pct,
        "cut_usdc": cut,
    }
    state["error_log"].append(
        f"REVENUE: deal {deal_id} fee ${assignment_fee_usd:.2f} -> swarm +{cut:.2f} USDC"
    )
    return {
        "status": "booked", "deal_id": deal_id, "cut_usdc": cut,
        "wallet_balance_usdc": fin["wallet_balance_usdc"],
        "revenue_generated_usdc": fin["revenue_generated_usdc"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tools.revenue",
        description="Book a closed wholesaling deal's cut into the swarm treasury.",
    )
    parser.add_argument("--deal-id", required=True, help="Unique deal id (idempotency key).")
    parser.add_argument("--assignment-fee", type=float, required=True,
                        help="Gross assignment fee in USD.")
    parser.add_argument("--cut-pct", type=float, default=DEFAULT_SWARM_CUT_PCT,
                        help=f"Swarm cut fraction (default {DEFAULT_SWARM_CUT_PCT}).")
    args = parser.parse_args(argv)

    # Imported here so the module stays importable without the full harness.
    import main as harness

    state = harness.load_state()
    result = book_closed_deal(
        state, deal_id=args.deal_id,
        assignment_fee_usd=args.assignment_fee, swarm_cut_pct=args.cut_pct,
    )
    harness.save_state(state)

    if result["status"] == "duplicate":
        print(f"[revenue] deal {args.deal_id} already booked ({result['cut_usdc']:.2f} USDC) — no change.")
    else:
        print(
            f"[revenue] booked deal {args.deal_id}: +{result['cut_usdc']:.2f} USDC "
            f"(treasury now {result['wallet_balance_usdc']:.2f}, "
            f"revenue {result['revenue_generated_usdc']:.2f})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
