"""tools/screener/harris.py — Harris County (HCAD) parcel adapter.

The county seam: cli.py looks adapters up by ``config.county``; adding a
county means writing this file's four functions for its GIS server and
registering it — no pipeline changes. Endpoint + field names live in config
(GIS servers move; the brief requires URLs be data, not code).

HCAD facts (verified against the published service):
  * layer: gis.hctx.net .../HCAD/Parcels/MapServer/0 (polygons, no token)
  * account field: HCAD_NUM — 13 digits, the APN with dashes stripped
  * owner names on the layer: owner_name_1..3
  * maxRecordCount 1000 (query_layer paginates past it)
"""

from __future__ import annotations

import json
import re
import time

from .arcgis import _request, query_layer
from .config import DEFAULT_CONFIG, ScreenerConfig

_APN_DIGITS = re.compile(r"\D+")


def normalize_apn(apn: str | None) -> str | None:
    """APN '044-024-000-0280' -> HCAD account '0440240000280' (13 digits)."""

    if not apn:
        return None
    digits = _APN_DIGITS.sub("", str(apn))
    return digits if len(digits) == 13 else None


def roads_config(config: ScreenerConfig = DEFAULT_CONFIG) -> tuple:
    """Ordered (url, name_fields) road-source candidates for this county."""

    return (
        (config.roads_arcgis_url, config.roads_name_fields),      # TxDOT
        (config.tiger_roads_url, config.tiger_roads_name_fields),  # Census
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
    """Parcel polygon + attributes for one account.

    Returns {"rings": [...], "owner": str, "gis_acreage": float|None} or
    {"error": ...} / {"missing": True}.
    """

    out_fields = ",".join(
        (config.hcad_account_field,) + config.hcad_owner_fields + ("Acreage",)
    )
    data = query_layer(
        config.hcad_parcels_url,
        {
            "where": f"{config.hcad_account_field}='{account}'",
            "outFields": out_fields,
            "returnGeometry": "true",
            "outSR": "4326",
        },
        http_request=http_request,
        sleep=sleep,
        retry_delays=config.retry_delays_s,
        cache=cache,
        cache_key=f"hcad:{account}",
    )
    if "error" in data:
        return {"error": data["error"]}
    features = data.get("features") or []
    if not features:
        return {"missing": True}
    feat = features[0]
    attrs = feat.get("attributes") or {}
    try:
        acreage = float(attrs.get("Acreage"))
    except (TypeError, ValueError):
        acreage = None
    return {
        "rings": _rings(feat),
        "owner": (attrs.get(config.hcad_owner_fields[0]) or "").strip(),
        "gis_acreage": acreage,
    }


def fetch_adjacent(
    account: str,
    rings_wgs84: list[list[tuple[float, float]]],
    config: ScreenerConfig = DEFAULT_CONFIG,
    *,
    http_request=_request,
    sleep=time.sleep,
    cache=None,
) -> list[dict]:
    """Neighboring parcels' owners — the assemblage-buyer list for slivers.

    Uses Intersects rather than Touches: parcel-fabric topology gaps make
    Touches drop true neighbors. The subject account is excluded after.
    Returns [{"owner": str, "account": str}] (may be empty; errors -> empty —
    adjacency is advisory, never worth failing a lead over).
    """

    geometry = json.dumps({
        "rings": [[list(pt) for pt in ring] for ring in rings_wgs84],
        "spatialReference": {"wkid": 4326},
    })
    out_fields = ",".join((config.hcad_account_field,) + config.hcad_owner_fields)
    data = query_layer(
        config.hcad_parcels_url,
        {
            "geometry": geometry,
            "geometryType": "esriGeometryPolygon",
            "spatialRel": "esriSpatialRelIntersects",
            "inSR": "4326",
            "outFields": out_fields,
            "returnGeometry": "false",
        },
        http_request=http_request,
        sleep=sleep,
        retry_delays=config.retry_delays_s,
        cache=cache,
        cache_key=f"hcad_adj:{account}",
    )
    if "error" in data:
        return []
    seen: set[str] = set()
    neighbors: list[dict] = []
    for feat in data.get("features") or []:
        attrs = feat.get("attributes") or {}
        acct = str(attrs.get(config.hcad_account_field) or "").strip()
        if not acct or acct == account or acct in seen:
            continue
        seen.add(acct)
        owner = next(
            (
                (attrs.get(f) or "").strip()
                for f in config.hcad_owner_fields
                if (attrs.get(f) or "").strip()
            ),
            "",
        )
        neighbors.append({"owner": owner, "account": acct})
    return neighbors
