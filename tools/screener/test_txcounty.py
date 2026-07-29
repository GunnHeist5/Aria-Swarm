"""Offline tests for the config-driven Texas county adapter — no network.

These pin the behaviour that matters when an endpoint entry is WRONG (the
likely case until each is verified on the VPS): the adapter must fail
closed — missing/error, never a fabricated parcel that could produce a bad
kill or a bad price.
"""

from __future__ import annotations

import json

from . import txcounty
from .config import DEFAULT_CONFIG

CONFIG = DEFAULT_CONFIG


def _stub(features, calls=None):
    def http(method, url, payload, key):
        if calls is not None:
            calls.append(url)
        return 200, json.dumps({"features": features})
    return http


def _feature(pid="123", owner="JANE DOE"):
    return {
        "attributes": {"PROP_ID": pid, "OWNER_NAME": owner, "Acreage": 1.5},
        "geometry": {"rings": [[[-94.8, 29.3], [-94.8, 29.31],
                                [-94.79, 29.31], [-94.79, 29.3],
                                [-94.8, 29.3]]]},
    }


def test_apn_keeps_its_punctuation_so_both_forms_stay_reachable():
    """Stripping here is lossy — dashes cannot be restored, so a layer that
    stores them could never be queried and every lead would read 'missing'."""

    assert txcounty.normalize_apn_for(
        "galveston", "1647-0005-0043-000", CONFIG) == "1647-0005-0043-000"
    assert txcounty.normalize_apn_for("galveston", "", CONFIG) is None


def test_fetch_parcel_returns_rings_and_owner():
    out = txcounty.fetch_parcel_for(
        "galveston", "1647-0005-0043-000", CONFIG,
        http_request=_stub([_feature()]), sleep=lambda _: None)
    assert out["owner"] == "JANE DOE"
    assert out["gis_acreage"] == 1.5
    assert len(out["rings"][0]) == 5


def test_unknown_county_is_an_explicit_error():
    try:
        txcounty.normalize_apn_for("nowhere", "123", CONFIG)
        assert False, "an unconfigured county must raise, not guess"
    except KeyError as exc:
        assert "nowhere" in str(exc)


def test_missing_parcel_fails_closed():
    """A wrong endpoint/key_field yields no features — the lead must route
    to needs_manual, never look like a screened parcel."""

    out = txcounty.fetch_parcel_for(
        "brazoria", "999", CONFIG,
        http_request=_stub([]), sleep=lambda _: None)
    assert out == {"missing": True}
    assert "rings" not in out


def test_fetch_parcel_retries_with_the_stripped_apn():
    """A CAD that stores accounts WITHOUT punctuation must still be
    reachable from the punctuated export value."""

    seen = []

    def http(method, url, payload, key):
        seen.append(url)
        # only the dash-free form matches this layer
        if "164700050043000" in url:
            return 200, json.dumps({"features": [_feature()]})
        return 200, json.dumps({"features": []})

    out = txcounty.fetch_parcel_for(
        "galveston", "1647-0005-0043-000", CONFIG,
        http_request=http, sleep=lambda _: None)
    assert out.get("owner") == "JANE DOE"
    assert len(seen) == 2, "tries as exported, then stripped"


def test_adjacent_owners_exclude_self_and_dedupe():
    features = [_feature("123", "SELF"), _feature("456", "NEIGHBOR A"),
                _feature("456", "NEIGHBOR A"), _feature("789", "BUILDER LLC")]
    out = txcounty.fetch_adjacent_for(
        "galveston", "123", [[(-94.8, 29.3)]], CONFIG,
        http_request=_stub(features), sleep=lambda _: None)
    assert [o["account"] for o in out] == ["456", "789"]


def test_adjacency_failure_is_advisory_not_fatal():
    def boom(method, url, payload, key):
        return 500, "server error"

    assert txcounty.fetch_adjacent_for(
        "galveston", "123", [[(-94.8, 29.3)]], CONFIG,
        http_request=boom, sleep=lambda _: None) == []


def test_adapter_shape_matches_the_hand_written_ones():
    from .cli import COUNTY_ADAPTERS

    for name in ("galveston", "brazoria", "chambers", "liberty"):
        adapter = COUNTY_ADAPTERS[name]
        for fn in ("normalize_apn", "fetch_parcel", "fetch_adjacent",
                   "roads_config"):
            assert hasattr(adapter, fn), f"{name} adapter missing {fn}"
        assert len(adapter.roads_config(CONFIG)) == 2


def test_coastal_counties_are_wired_into_enrichment():
    """The whole point: Galveston/Brazoria leads must stop being emailed
    with no flood screen."""

    from ..acquisition.enrich import ADAPTER_BY_MARKET

    assert ADAPTER_BY_MARKET["galveston_tx"] == "galveston"
    assert ADAPTER_BY_MARKET["brazoria_tx"] == "brazoria"
