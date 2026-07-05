"""tools/dealdesk/test_pricing.py — regression tests for the pricing band.

Safety-critical money logic: the ceiling Aria may never exceed. Uses fabricated
PropertyRecords (no PII export needed) so it runs anywhere. Run:
    python -m pytest tools/dealdesk/test_pricing.py    (or: python tools/dealdesk/test_pricing.py)
"""

from __future__ import annotations

import importlib
import os

from .lookup import PropertyRecord


def _rec(**kw) -> PropertyRecord:
    base = dict(
        address="1 Test St", city="Houston", state="TX", zip="77000",
        county="Harris", apn="000-000-000-0001", est_value=None,
        assessed_value=None, open_loans_balance=None, lien_amount=None,
        mls_status=None, property_type="Vacant Land (General)", raw={},
    )
    base.update(kw)
    return PropertyRecord(**base)


def _pricing(**env):
    """Reload the pricing module so module-level env knobs re-read."""

    for k in ("DEALDESK_ARV_BASIS", "DEALDESK_ASSIGNMENT_FEE_PCT",
              "DEALDESK_ASSIGNMENT_FEE_USD", "DEALDESK_MAX_AUTONOMOUS_OFFER_USD",
              "DEALDESK_ARV_MULTIPLIER", "DEALDESK_MIN_VIABLE_OFFER_USD"):
        os.environ.pop(k, None)
    os.environ.update(env)
    import tools.dealdesk.pricing as pricing
    return importlib.reload(pricing)


def test_lower_of_picks_the_smaller_valuation():
    p = _pricing(DEALDESK_ARV_BASIS="lower_of", DEALDESK_ASSIGNMENT_FEE_PCT="0.10")
    # est 334k vs assessed 170k -> ceiling built on 170k (Beulah numbers).
    band = p.compute_offer_range(_rec(est_value=334000, assessed_value=170000))
    assert not band["escalate"], band
    # 170000*0.70 = 119000; fee 10% of resale = 11900; ceiling 107100.
    assert band["max_offer"] == 107100.0, band
    assert band["opening_offer"] == 91000.0, band  # round100(107100*0.85)


def test_est_value_basis_uses_the_high_number():
    p = _pricing(DEALDESK_ARV_BASIS="est_value", DEALDESK_ASSIGNMENT_FEE_PCT="0.10")
    band = p.compute_offer_range(_rec(est_value=334000, assessed_value=170000))
    # 334000*0.70*0.90 = 210420 -> round to nearest $100 = 210400.
    assert band["max_offer"] == 210400.0, band


def test_percentage_fee_scales_down_on_a_cheap_lot():
    p = _pricing(DEALDESK_ARV_BASIS="lower_of", DEALDESK_ASSIGNMENT_FEE_PCT="0.10")
    # A $4,725 assessed lot survives (flat $15k fee would kill it).
    band = p.compute_offer_range(_rec(est_value=206000, assessed_value=4725))
    assert not band["escalate"], band
    # 4725*0.70*0.90 = 2976.75 -> ceiling rounds to 3000, opening 2500.
    assert band["max_offer"] == 3000.0, band


def test_flat_fee_when_pct_disabled():
    p = _pricing(DEALDESK_ASSIGNMENT_FEE_PCT="0", DEALDESK_ASSIGNMENT_FEE_USD="4000",
                 DEALDESK_ARV_BASIS="lower_of")
    band = p.compute_offer_range(_rec(est_value=334000, assessed_value=170000))
    # 170000*0.70 - 4000 = 115000.
    assert band["max_offer"] == 115000.0, band


def test_high_value_gate_escalates_whales():
    p = _pricing(DEALDESK_MAX_AUTONOMOUS_OFFER_USD="250000",
                 DEALDESK_ASSIGNMENT_FEE_PCT="0.10", DEALDESK_ARV_BASIS="est_value")
    # est 4M -> ceiling ~2.5M > cap -> escalate.
    band = p.compute_offer_range(_rec(est_value=4_000_000, assessed_value=4_000_000))
    assert band["escalate"] and band["escalate_reason"] == "high_value", band


def test_no_valuation_escalates():
    p = _pricing()
    band = p.compute_offer_range(_rec(est_value=None, assessed_value=None))
    assert band["escalate"] and band["escalate_reason"] == "no_valuation", band


def test_listed_with_agent_escalates():
    p = _pricing()
    band = p.compute_offer_range(_rec(est_value=100000, assessed_value=100000,
                                      mls_status="ACTIVE"))
    assert band["escalate"] and band["escalate_reason"] == "listed_with_agent", band


def test_encumbered_escalates():
    p = _pricing(DEALDESK_ASSIGNMENT_FEE_PCT="0.10", DEALDESK_ARV_BASIS="lower_of")
    # owed >= ceiling -> can't clear title on a cash purchase.
    band = p.compute_offer_range(_rec(est_value=100000, assessed_value=100000,
                                      open_loans_balance=90000))
    assert band["escalate"] and band["escalate_reason"] == "encumbered", band


def test_not_land_escalates():
    p = _pricing()
    band = p.compute_offer_range(_rec(est_value=100000, property_type="Single Family"))
    assert band["escalate"] and band["escalate_reason"] == "not_land", band


def test_response_never_leaks_arv_or_margin():
    p = _pricing(DEALDESK_ASSIGNMENT_FEE_PCT="0.10")
    band = p.compute_offer_range(_rec(est_value=200000, assessed_value=150000))
    for forbidden in ("arv", "margin", "assessed", "est_value", "fee"):
        assert forbidden not in band, f"{forbidden} leaked: {band}"


def test_not_found_escalates():
    p = _pricing()
    band = p.compute_offer_range(None)
    assert band["escalate"] and band["escalate_reason"] == "not_found"
    assert band["found"] is False


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")
