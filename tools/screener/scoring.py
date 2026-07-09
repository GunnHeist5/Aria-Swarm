"""tools/screener/scoring.py — verdict + MAO decision table (PURE, stage 6).

No I/O and no config reads outside the passed genome, so a fee-threshold
tweak re-scores a cached run instantly without refetching anything.

Verdict precedence (brief §6):
  needs_manual        -> blank verdict (separate sheet)
  SLIVER + builder-looking neighbor -> ASSEMBLAGE_LEAD (sell the strip to
                         the adjacent developer instead of passing)
  any hard kill       -> PASS
  no usable comps     -> needs_manual
  floodplain          -> RENEGOTIATE (valuation capped, cut % suggested)
  mao >= asking       -> SEND CONTRACT
  otherwise           -> NEGOTIATE (open_at / walk_at band)
"""

from __future__ import annotations

import re
from collections import Counter

from .config import DEFAULT_CONFIG, ScreenerConfig
from .geometry import SLIVER

BUILDER_RE = re.compile(
    r"\b(L\.?L\.?C\.?|INC(ORPORATED)?|CORP(ORATION)?|LTD|L\.?P\.?|HOMES?|"
    r"BUILDERS?|BUILDING|DEVELOPMENT|DEVELOPERS?|PROPERTIES|INVESTMENTS?|"
    r"VENTURES?|HOLDINGS?|CAPITAL|GROUP|PARTNERS(HIP)?|TRUST|LAND\s+CO)\b",
    re.IGNORECASE,
)


def assemblage_candidates(adjacent: list[dict]) -> list[str]:
    """Builder/developer-looking adjacent owners — the assemblage-buyer list.

    A hit is a builder-word in the owner name, or the same entity owning
    two or more adjacent parcels (someone quietly assembling the block).
    """

    names = [(a.get("owner") or "").strip() for a in adjacent]
    names = [n for n in names if n]
    hits = {n for n in names if BUILDER_RE.search(n)}
    counts = Counter(names)
    hits |= {n for n, c in counts.items() if c >= 2}
    return sorted(hits)


def score_row(row: dict, config: ScreenerConfig = DEFAULT_CONFIG) -> dict:
    """Compute buyer_ceiling / mao / verdict / open_at / walk_at for one row."""

    out = {
        "buyer_ceiling": None,
        "mao": None,
        "verdict": "",
        "open_at": None,
        "walk_at": None,
    }
    if row.get("needs_manual_reason"):
        return out

    sliver = row.get("shape_flag") == SLIVER
    landlocked = row.get("frontage") == "NONE"
    if sliver or landlocked:
        if sliver:
            builders = assemblage_candidates(row.get("_adjacent") or [])
            if builders:
                out["verdict"] = "ASSEMBLAGE_LEAD"
                return out
        out["verdict"] = "PASS"
        return out

    retail = row.get("retail_estimate")
    if retail is None:
        row["needs_manual_reason"] = "no usable comps"
        return out

    flooded = row.get("flood_flag") == "FLOODPLAIN"
    effective_retail = retail * (1 - config.flood_cut_pct) if flooded else retail
    ceiling = round(effective_retail * config.buyer_ceiling_ratio)
    mao = round(ceiling - config.target_fee_usd)
    out["buyer_ceiling"] = ceiling
    out["mao"] = mao

    if flooded:
        out["verdict"] = "RENEGOTIATE"
        note = (
            f"floodplain {row.get('flood_zone', '?')}: "
            f"suggest {config.flood_cut_pct:.0%} price cut"
        )
        prior = row.get("comp_evidence") or ""
        row["comp_evidence"] = f"{prior} | {note}" if prior else note
        out["open_at"] = round(mao * config.open_at_ratio)
        out["walk_at"] = round(ceiling - config.min_fee_usd)
        return out

    asking = row.get("_asking")
    if asking is not None and mao >= asking:
        out["verdict"] = "SEND CONTRACT"
        return out

    out["verdict"] = "NEGOTIATE"
    out["open_at"] = round(mao * config.open_at_ratio)
    out["walk_at"] = round(ceiling - config.min_fee_usd)
    return out
