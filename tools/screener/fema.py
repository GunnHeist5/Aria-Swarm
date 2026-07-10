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
        # * not a field list: the AGOL mirror's schema differs slightly from
        # FEMA's (no SFHA_TF), and naming a missing field errors the query.
        "outFields": "*",
        "returnGeometry": "false",
    }
    data, source = _query_nfhl(
        params, config, http_request=http_request, sleep=sleep, cache=cache,
        cache_key=f"fema:{lat:.5f},{lon:.5f}")
    if "error" in data:
        return {"error": data["error"]}
    features = data.get("features") or []
    if not features:
        return {"flood_zone": "UNKNOWN", "flood_flag": None, "source": source}
    attrs = features[0].get("attributes") or {}
    zone = (attrs.get("FLD_ZONE") or "UNKNOWN").strip()
    subtype = (attrs.get("ZONE_SUBTY") or "").strip()
    label = f"{zone} ({subtype})" if subtype else zone
    return {
        "flood_zone": label,
        "flood_flag": classify_zone(zone, attrs.get("SFHA_TF")),
        "source": source,
    }


def _query_nfhl(params: dict, config: ScreenerConfig, *, http_request, sleep,
                cache=None, cache_key=None) -> tuple[dict, str | None]:
    """Try each configured NFHL host in order; (response, answering host)."""

    data: dict = {"error": "no FEMA host configured"}
    for host in (config.fema_nfhl_url, config.fema_nfhl_fallback_url,
                 config.fema_agol_fallback_url):
        if not host:
            continue
        data = query_layer(
            host,
            params,
            http_request=http_request,
            sleep=sleep,
            retry_delays=config.retry_delays_s,
            cache=cache,
            cache_key=cache_key,
        )
        if "error" not in data:
            return data, host
    return data, None


def sfha_count_in_envelope(
    bbox_wgs84: tuple[float, float, float, float],
    config: ScreenerConfig = DEFAULT_CONFIG,
    *,
    http_request=_request,
    sleep=time.sleep,
) -> dict:
    """How many A/V-zone polygons intersect (w, s, e, n)? For the self-check.

    A data-presence probe: it validates the reachable host actually carries
    Special Flood Hazard Area polygons for the area, without betting the
    check on one hand-guessed coordinate being inside a flood line.
    """

    w, s, e, n = bbox_wgs84
    import json as _json

    params = {
        "geometry": _json.dumps({"xmin": w, "ymin": s, "xmax": e, "ymax": n,
                                 "spatialReference": {"wkid": 4326}}),
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "where": "FLD_ZONE LIKE 'A%' OR FLD_ZONE LIKE 'V%'",
        "returnCountOnly": "true",
    }
    data, source = _query_nfhl(params, config, http_request=http_request,
                               sleep=sleep)
    if "error" in data:
        return {"error": data["error"]}
    return {"count": int(data.get("count") or 0), "source": source}
