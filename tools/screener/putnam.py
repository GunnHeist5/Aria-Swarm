"""tools/screener/putnam.py — Putnam County, FL parcel adapter.

Parcels come from the Florida statewide cadastral layer (FGIO republishing
the FDOR NAL roll) on ArcGIS Online — the same infrastructure class as the
Harris/TxDOT layers. Facts that matter (researched, live-verified by
``screen.py --check --county putnam`` on the VPS):

  * PARCELNO is only unique WITHIN a county — every query must also filter
    ``CO_NO = 64`` (FDOR's number for Putnam; 54 is Monroe, don't guess).
  * PropStream exports Putnam APNs dashed (``11-10-23-9303-0020-0230``);
    FDOR *probably* stores them dash-stripped, but that's inferred, so
    ``fetch_parcel`` tries the stripped form first and retries dashed.
  * Owner name lives in ``OWN_NAME``.

Roads are FDOT's FLARIS All-Roads-Basemap streets (state+county+local+
private), selected via ``roads_config`` — the county seam that lets the
frontage stage stay county-agnostic.
"""

from __future__ import annotations

import json
import re
import time

from .arcgis import _request, query_layer
from .config import DEFAULT_CONFIG, ScreenerConfig

_DIGITS = re.compile(r"\D+")

# FDOR two-digit county number for Putnam (alphabetical from Alachua=11).
CO_NO = 64


def normalize_apn(apn: str | None) -> str | None:
    """'11-10-23-9303-0020-0230' -> '111023930300200230' (18 digits)."""

    if not apn:
        return None
    digits = _DIGITS.sub("", str(apn))
    return digits if len(digits) == 18 else None


def _dashed(account: str) -> str:
    """Rebuild PropStream's dashed form from the 18-digit account."""

    parts = (account[0:2], account[2:4], account[4:6], account[6:10],
             account[10:14], account[14:18])
    return "-".join(parts)


def roads_config(config: ScreenerConfig = DEFAULT_CONFIG) -> tuple:
    """Ordered (url, name_fields) road-source candidates for this county.

    Census TIGER first: reachable (FDOT's host WAF-blocked the VPS) and
    returns real street names. FLARIS stays as the richer backup for
    whenever its WAF relents.
    """

    return (
        (config.tiger_roads_url, config.tiger_roads_name_fields),   # Census
        (config.fl_roads_arcgis_url, config.fl_roads_name_fields),  # FDOT
    )


def _rings(feature: dict) -> list[list[tuple[float, float]]]:
    return [
        [tuple(pt[:2]) for pt in ring]
        for ring in (feature.get("geometry") or {}).get("rings") or []
    ]


def fetch_parcel(
    account: str,
    config: ScreenerConfig = DEFAULT_CONFIG,
    *,
    http_request=_request,
    sleep=time.sleep,
    cache=None,
) -> dict:
    """Parcel polygon + owner for one account (stripped, then dashed retry)."""

    for candidate in (account, _dashed(account)):
        data = query_layer(
            config.fl_cadastral_url,
            {
                "where": f"CO_NO = {CO_NO} AND PARCELNO = '{candidate}'",
                "outFields": "PARCELNO,OWN_NAME,CO_NO",
                "returnGeometry": "true",
                "outSR": "4326",
            },
            http_request=http_request,
            sleep=sleep,
            retry_delays=config.retry_delays_s,
            cache=cache,
            cache_key=f"flcad:{candidate}",
        )
        if "error" in data:
            return {"error": data["error"]}
        features = data.get("features") or []
        if features:
            feat = features[0]
            attrs = feat.get("attributes") or {}
            return {
                "rings": _rings(feat),
                "owner": (attrs.get("OWN_NAME") or "").strip(),
                "gis_acreage": None,  # not on the NAL join; area comes from geometry
            }
    return {"missing": True}


def fetch_adjacent(
    account: str,
    rings_wgs84: list[list[tuple[float, float]]],
    config: ScreenerConfig = DEFAULT_CONFIG,
    *,
    http_request=_request,
    sleep=time.sleep,
    cache=None,
) -> list[dict]:
    """Neighboring owners via polygon intersect (CO_NO-scoped)."""

    geometry = json.dumps({
        "rings": [[list(pt) for pt in ring] for ring in rings_wgs84],
        "spatialReference": {"wkid": 4326},
    })
    data = query_layer(
        config.fl_cadastral_url,
        {
            "geometry": geometry,
            "geometryType": "esriGeometryPolygon",
            "spatialRel": "esriSpatialRelIntersects",
            "inSR": "4326",
            "where": f"CO_NO = {CO_NO}",
            "outFields": "PARCELNO,OWN_NAME",
            "returnGeometry": "false",
        },
        http_request=http_request,
        sleep=sleep,
        retry_delays=config.retry_delays_s,
        cache=cache,
        cache_key=f"flcad_adj:{account}",
    )
    if "error" in data:
        return []
    self_keys = {account, _dashed(account)}
    seen: set[str] = set()
    neighbors: list[dict] = []
    for feat in data.get("features") or []:
        attrs = feat.get("attributes") or {}
        parcel = str(attrs.get("PARCELNO") or "").strip()
        if not parcel or parcel in self_keys or parcel in seen:
            continue
        seen.add(parcel)
        owner = (attrs.get("OWN_NAME") or "").strip()
        neighbors.append({"owner": owner, "account": parcel})
    return neighbors
