"""tools/screener/fema.py — FEMA National Flood Hazard Layer lookup.

Point-in-polygon query of the parcel centroid against NFHL layer 28 (Flood
Hazard Zones). A-family and V-family zones (or SFHA_TF='T') are the Special
Flood Hazard Area -> soft kill: the lead survives but valuation is capped and
the flag becomes a negotiation lever. X / shaded X pass.
"""

from __future__ import annotations

import time

from .arcgis import _request, query_layer
from .config import DEFAULT_CONFIG, ScreenerConfig

FLOODPLAIN = "FLOODPLAIN"


def classify_zone(fld_zone: str | None, sfha_tf: str | None = None) -> str | None:
    """PURE. FEMA zone code -> FLOODPLAIN flag or None (pass)."""

    zone = (fld_zone or "").strip().upper()
    if (sfha_tf or "").strip().upper() == "T":
        return FLOODPLAIN
    if zone.startswith("A") or zone.startswith("V"):
        return FLOODPLAIN
    return None


def flood_zone(
    lat: float,
    lon: float,
    config: ScreenerConfig = DEFAULT_CONFIG,
    *,
    http_request=_request,
    sleep=time.sleep,
    cache=None,
) -> dict:
    """{"flood_zone": code, "flood_flag": FLOODPLAIN|None} or {"error": ...}.

    A point outside any mapped panel returns zone UNKNOWN with no flag —
    unmapped is not a floodplain claim, and flood is a soft kill anyway.
    """

    params = {
        "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "FLD_ZONE,ZONE_SUBTY,SFHA_TF",
        "returnGeometry": "false",
    }
    data: dict = {"error": "no FEMA host configured"}
    for host in (config.fema_nfhl_url, config.fema_nfhl_fallback_url):
        if not host:
            continue
        data = query_layer(
            host,
            params,
            http_request=http_request,
            sleep=sleep,
            retry_delays=config.retry_delays_s,
            cache=cache,
            cache_key=f"fema:{lat:.5f},{lon:.5f}",
        )
        if "error" not in data:
            break
    if "error" in data:
        return {"error": data["error"]}
    features = data.get("features") or []
    if not features:
        return {"flood_zone": "UNKNOWN", "flood_flag": None}
    attrs = features[0].get("attributes") or {}
    zone = (attrs.get("FLD_ZONE") or "UNKNOWN").strip()
    subtype = (attrs.get("ZONE_SUBTY") or "").strip()
    label = f"{zone} ({subtype})" if subtype else zone
    return {
        "flood_zone": label,
        "flood_flag": classify_zone(zone, attrs.get("SFHA_TF")),
    }
