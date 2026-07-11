"""tools/screener/check.py — live endpoint self-check (contracts.py --check
convention: prove the path before real leads hit it).

Run ON THE VPS (the dev sandbox's egress can't reach GIS hosts), once per
county you screen:

    python screen.py --check                     # harris (default)
    python screen.py --check --county putnam

Probes per county:
  * parcels: a known account must return a polygon with computable metrics
    and adjacent owner names (Harris additionally asserts its acceptance
    parcel is a SLIVER);
  * FEMA: a reachable NFHL host must carry A/V-zone polygons in an envelope
    around the county's town-center point, and a point lookup must run
    end-to-end;
  * roads: the state's roadway-inventory layer must return named roads for a
    road-dense bbox (Overpass reported as informational fallback);
  * Brave: key validity — skipped if not configured.

Exit 0 iff every probe (except explicitly skipped ones) passes.
"""

from __future__ import annotations

import json

from . import fema, frontage
from .config import DEFAULT_CONFIG, ScreenerConfig
from .geometry import parcel_metrics, shape_flag

# Per-county probe fixtures. `account` is a real parcel in the county
# (normalized form the adapter expects); `center` is a road-dense point near
# mapped floodplain (Hunting Bayou / the St. Johns River at Palatka).
COUNTY_PROBES = {
    "harris": {
        "account": "0440240000280",  # Richland Dr strip (live: ~48x217, AR 4.5)
        "expect_sliver": True,
        "center": (29.8280, -95.2861),
    },
    "putnam": {
        "account": "111023930300200230",  # 11-10-23-9303-0020-0230 (PropStream)
        "expect_sliver": False,
        "center": (29.6486, -81.6376),    # Palatka
    },
}


def run_check(config: ScreenerConfig = DEFAULT_CONFIG) -> int:
    from .cli import COUNTY_ADAPTERS

    county = COUNTY_ADAPTERS[config.county]
    spec = COUNTY_PROBES[config.county]
    account, center = spec["account"], spec["center"]
    report: dict = {"county": config.county, "probes": {}}
    ok = True

    # -- parcel + geometry ----------------------------------------------------
    parcel = county.fetch_parcel(account, config)
    probe: dict = {"account": account}
    if parcel.get("error") or parcel.get("missing"):
        probe.update(ok=False, detail=parcel.get("error") or "not found")
        ok = False
    else:
        metrics = parcel_metrics(parcel["rings"]) or {}
        flag = shape_flag(
            metrics.get("width_ft", 0), metrics.get("aspect_ratio", 0), config)
        adjacent = county.fetch_adjacent(account, parcel["rings"], config)
        shape_ok = flag == "SLIVER" if spec["expect_sliver"] else bool(metrics)
        probe.update(
            ok=shape_ok and bool(adjacent),
            owner=parcel.get("owner"),
            width_ft=metrics.get("width_ft"),
            depth_ft=metrics.get("depth_ft"),
            aspect_ratio=metrics.get("aspect_ratio"),
            shape_flag=flag,
            adjacent_owners=[a["owner"] for a in adjacent][:8],
        )
        ok = ok and probe["ok"]
    report["probes"]["parcel"] = probe

    # -- FEMA flood data ------------------------------------------------------
    # Data-presence probe: the center point sits near a major waterway, so
    # the flood layer MUST carry A/V-zone polygons in this envelope. This
    # validates host + query path without betting on one hand-picked
    # coordinate sitting exactly inside a flood line.
    d = 0.025
    envelope = (center[1] - d, center[0] - d, center[1] + d, center[0] + d)
    sfha = fema.sfha_count_in_envelope(envelope, config)
    if sfha.get("error"):
        probe = {"ok": False, "detail": sfha["error"]}
    else:
        probe = {"ok": sfha["count"] > 0, "sfha_polygons": sfha["count"],
                 "source": sfha.get("source")}
    ok = ok and bool(probe["ok"])
    report["probes"]["fema_sfha"] = probe

    # Point lookup exercised end-to-end (zone value is informational — the
    # exact code at a guessed point isn't an acceptance criterion).
    zone = fema.flood_zone(*center, config)
    if zone.get("error"):
        probe = {"ok": False, "detail": zone["error"]}
        ok = False
    else:
        probe = {"ok": True, "flood_zone": zone.get("flood_zone"),
                 "flood_flag": zone.get("flood_flag"),
                 "source": zone.get("source")}
    report["probes"]["fema_point_lookup"] = probe

    # -- Roads ---------------------------------------------------------------
    # Probe a road-dense town-center bbox, NOT a parcel's own bbox — a parcel
    # with no nearby road is the landlocked case, not a probe failure. The
    # state roadway inventory is primary; Overpass is informational fallback.
    d = 0.002
    bbox = (center[1] - d, center[0] - d, center[1] + d, center[0] + d)
    any_roads = False
    candidates = {}
    for roads_url, name_fields in county.roads_config(config):
        host = roads_url.split("/")[2]
        roads = frontage.fetch_roads_arcgis(
            bbox, config, url=roads_url, name_fields=name_fields)
        if roads is None:
            candidates[host] = {"ok": False, "detail": "fetch failed"}
        elif not roads:
            candidates[host] = {"ok": False, "detail": "no roads returned"}
        else:
            # Names are not enough — a wrong coordinate system returns nice
            # names with roads on another continent (seen live: every parcel
            # "landlocked"). The first vertex must be near the probe center.
            lon, lat = roads[0]["coords"][0]
            near = abs(lon - center[1]) < 0.1 and abs(lat - center[0]) < 0.1
            candidates[host] = {
                "ok": near,
                "roads": sorted({r["name"] for r in roads})[:6],
                "coords_geographic": near,
                "sample_vertex": [round(lon, 4), round(lat, 4)],
            }
            any_roads = any_roads or near
    report["probes"]["road_sources"] = candidates
    overpass = frontage.fetch_roads(bbox, config)
    report["probes"]["overpass_fallback"] = (
        {"ok": None, "detail": "unreachable (fallback only)"}
        if overpass is None
        else {"ok": bool(overpass), "roads_found": len(overpass)}
    )
    # The pipeline needs at least ONE working road source.
    ok = ok and (any_roads or bool(overpass))

    # -- Brave key ------------------------------------------------------------
    try:
        from tools.integrations.secrets import get_secret

        brave_key = get_secret("BRAVE_API_KEY") or get_secret("SEARCH_API_KEY")
    except Exception:
        brave_key = None
    if brave_key:
        from .comps import BraveClient

        results = BraveClient(brave_key, config).search("vacant land price per acre")
        probe = {"ok": bool(results), "results": len(results)}
        ok = ok and probe["ok"]
    else:
        probe = {"ok": None, "detail": "skipped: no BRAVE_API_KEY/SEARCH_API_KEY"}
    report["probes"]["brave_search"] = probe

    report["ok"] = ok
    print(json.dumps(report, indent=2))
    return 0 if ok else 1
