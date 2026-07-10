"""tools/screener/check.py — live endpoint self-check (contracts.py --check
convention: prove the path before real leads hit it).

Run ON THE VPS (the dev sandbox's egress can't reach GIS hosts):

    python screen.py --check

Probes, each mapped to a brief acceptance test:
  * HCAD: account 0440240000280 (Richland Dr, 77028) must return a polygon
    that computes to shape_flag=SLIVER with adjacent owner names.
  * FEMA: a known-floodplain 77028 point must classify FLOODPLAIN; a Tomball
    point must pass (X).
  * Overpass: named roads must come back near the 77028 parcel.
  * Brave: key validity (one cheap query) — skipped if no key configured.

Exit 0 iff every probe (except an explicitly skipped one) passes.
"""

from __future__ import annotations

import json

from . import fema, frontage, harris
from .config import DEFAULT_CONFIG, ScreenerConfig
from .geometry import parcel_metrics, shape_flag

SLIVER_ACCOUNT = "0440240000280"  # Richland Dr strip (live: ~48x217, AR 4.5)
FLOOD_POINT = (29.8280, -95.2861)          # Hunting Bayou area, 77028
DRY_POINT = (30.0906, -95.6592)            # Tomball


def run_check(config: ScreenerConfig = DEFAULT_CONFIG) -> int:
    report: dict = {"probes": {}}
    ok = True

    # -- HCAD parcel + SLIVER acceptance ------------------------------------
    parcel = harris.fetch_parcel(SLIVER_ACCOUNT, config)
    probe: dict = {"account": SLIVER_ACCOUNT}
    if parcel.get("error") or parcel.get("missing"):
        probe.update(ok=False, detail=parcel.get("error") or "not found")
        ok = False
    else:
        metrics = parcel_metrics(parcel["rings"]) or {}
        flag = shape_flag(
            metrics.get("width_ft", 0), metrics.get("aspect_ratio", 0), config)
        adjacent = harris.fetch_adjacent(SLIVER_ACCOUNT, parcel["rings"], config)
        probe.update(
            ok=flag == "SLIVER" and bool(adjacent),
            owner=parcel.get("owner"),
            width_ft=metrics.get("width_ft"),
            depth_ft=metrics.get("depth_ft"),
            aspect_ratio=metrics.get("aspect_ratio"),
            shape_flag=flag,
            adjacent_owners=[a["owner"] for a in adjacent][:8],
        )
        ok = ok and probe["ok"]
    report["probes"]["hcad_sliver"] = probe

    # -- FEMA flood zones -----------------------------------------------------
    for name, (lat, lon), expect_flag in (
        ("fema_floodplain_77028", FLOOD_POINT, True),
        ("fema_dry_tomball", DRY_POINT, False),
    ):
        zone = fema.flood_zone(lat, lon, config)
        if zone.get("error"):
            probe = {"ok": False, "detail": zone["error"]}
            ok = False
        else:
            flagged = zone.get("flood_flag") == "FLOODPLAIN"
            probe = {
                "ok": flagged == expect_flag,
                "flood_zone": zone.get("flood_zone"),
                "expected": "FLOODPLAIN" if expect_flag else "pass",
            }
            ok = ok and probe["ok"]
        report["probes"][name] = probe

    # -- Overpass roads ------------------------------------------------------
    # Probe a road-dense residential bbox, NOT the sliver parcel's own bbox —
    # a sliver with no nearby road is the landlocked case, not a probe failure.
    d = 0.002
    bbox = (FLOOD_POINT[1] - d, FLOOD_POINT[0] - d,
            FLOOD_POINT[1] + d, FLOOD_POINT[0] + d)
    roads = frontage.fetch_roads(bbox, config)
    if roads is None:
        probe = {"ok": False, "detail": "fetch failed on both Overpass hosts"}
    else:
        probe = {
            "ok": bool(roads),
            "roads": sorted({r["name"] for r in roads})[:8],
        }
    ok = ok and bool(probe["ok"])
    report["probes"]["overpass_roads"] = probe

    # -- Brave key ------------------------------------------------------------
    try:
        from tools.integrations.secrets import get_secret

        brave_key = get_secret("BRAVE_API_KEY") or get_secret("SEARCH_API_KEY")
    except Exception:
        brave_key = None
    if brave_key:
        from .comps import BraveClient

        results = BraveClient(brave_key, config).search("77028 vacant land price")
        probe = {"ok": bool(results), "results": len(results)}
        ok = ok and probe["ok"]
    else:
        probe = {"ok": None, "detail": "skipped: no BRAVE_API_KEY/SEARCH_API_KEY"}
    report["probes"]["brave_search"] = probe

    report["ok"] = ok
    print(json.dumps(report, indent=2))
    return 0 if ok else 1
