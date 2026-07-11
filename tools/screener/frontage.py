"""tools/screener/frontage.py — road frontage check (landlocked kill flag).

Two halves, split for testability:
  * ``fetch_roads`` — Overpass API (OpenStreetMap) call for road centerlines
    in the parcel's expanded bounding box. Overpass's primary host bounces
    programmatic traffic, so the default is a mirror with the primary as
    fallback, with backoff on 429/504.
  * ``compute_frontage`` — PURE: buffer the parcel by ~5 ft in local feet and
    intersect with each road polyline; the longest shared edge wins.

FAIL-CLOSED RULE: "landlocked" is a kill flag, so a road-data fetch failure
must never produce frontage=NONE — callers route fetch errors to
needs_manual instead.
"""

from __future__ import annotations

import json
import time
import urllib.request

from shapely.geometry import LineString

from .arcgis import USER_AGENT, open_with_tls_fallback
from .config import DEFAULT_CONFIG, ScreenerConfig
from .geometry import _polygon_from_rings, feet_per_degree, to_local_feet

# Ways that never grant legal/practical vehicle access.
_EXCLUDED_HIGHWAYS = "footway|cycleway|path|track|steps|bridleway|corridor"
_BBOX_PAD_FT = 150.0


def _overpass_request(method: str, url: str, payload: dict | None, key: str) -> tuple[int, str]:
    """POST the Overpass QL body (plain text, not form-encoded)."""

    req = urllib.request.Request(
        url,
        data=(payload or {}).get("data", "").encode("utf-8"),
        headers={"User-Agent": USER_AGENT, "Content-Type": "text/plain"},
        method="POST",
    )
    return open_with_tls_fallback(req, timeout=60)


def fetch_roads(
    bbox_wgs84: tuple[float, float, float, float],
    config: ScreenerConfig = DEFAULT_CONFIG,
    *,
    http_request=_overpass_request,
    sleep=time.sleep,
    cache=None,
) -> list[dict] | None:
    """Road centerlines near the parcel: [{"name": str, "coords": [(lon,lat)]}].

    Returns None on failure (NOT an empty list — empty means "verified no
    roads", None means "we couldn't check", and only the former may kill).
    """

    w, s, e, n = bbox_wgs84
    lat0 = (s + n) / 2
    ft_lon, ft_lat = feet_per_degree(lat0)
    pad_lon, pad_lat = _BBOX_PAD_FT / ft_lon, _BBOX_PAD_FT / ft_lat
    bbox = (
        round(s - pad_lat, 5), round(w - pad_lon, 5),
        round(n + pad_lat, 5), round(e + pad_lon, 5),
    )
    cache_key = "overpass:" + ",".join(f"{v:.5f}" for v in bbox)
    if cache is not None:
        hit = cache.get_http(cache_key)
        if hit is not None:
            return json.loads(hit)

    query = (
        f'[out:json][timeout:25];'
        f'way["highway"]["highway"!~"{_EXCLUDED_HIGHWAYS}"]'
        f'({bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]});'
        f'out tags geom;'
    )
    data = None
    for host in config.overpass_urls:
        sleep(config.overpass_spacing_s)
        for attempt, delay in enumerate((0.0,) + tuple(config.retry_delays_s)):
            if delay:
                sleep(delay)
            status, resp = http_request("POST", host, {"data": query}, "")
            if status == 200:
                try:
                    parsed = json.loads(resp)
                except ValueError:
                    break  # garbage from this host — try the next
                # Overpass reports overload as HTTP 200 + a "remark" (e.g.
                # "runtime error: Query timed out") — that is NOT "verified
                # no roads"; treating it as such would false-kill the lead.
                remark = str(parsed.get("remark") or "")
                if "error" in remark.lower() or "timed out" in remark.lower():
                    break
                data = parsed
                break
            if status not in (429, 504, 0):
                break  # non-transient — try the next host
        if data is not None:
            break
    if data is None:
        return None

    elements = data.get("elements") or []
    roads = [
        {
            "name": (el.get("tags") or {}).get("name") or "(unnamed road)",
            "coords": [(pt["lon"], pt["lat"]) for pt in el.get("geometry") or []],
        }
        for el in elements
        if el.get("type") == "way" and len(el.get("geometry") or []) >= 2
    ]
    if cache is not None:
        cache.put_http(cache_key, 200, json.dumps(roads))
    return roads


def fetch_roads_arcgis(
    bbox_wgs84: tuple[float, float, float, float],
    config: ScreenerConfig = DEFAULT_CONFIG,
    *,
    url: str | None = None,
    name_fields: tuple | None = None,
    http_request=None,
    sleep=time.sleep,
    cache=None,
) -> list[dict] | None:
    """Road centerlines from a state roadway-inventory layer (ArcGIS polylines).

    Defaults to the TxDOT layer; county adapters pass their state's layer via
    ``url``/``name_fields`` (see ``<adapter>.roads_config``). Same contract as
    fetch_roads: None = couldn't check, [] = verified none.
    """

    from .arcgis import _request, query_layer

    url = url or config.roads_arcgis_url
    name_fields = name_fields or config.roads_name_fields
    w, s, e, n = bbox_wgs84
    lat0 = (s + n) / 2
    ft_lon, ft_lat = feet_per_degree(lat0)
    pad_lon, pad_lat = _BBOX_PAD_FT / ft_lon, _BBOX_PAD_FT / ft_lat
    envelope = json.dumps({
        "xmin": round(w - pad_lon, 5), "ymin": round(s - pad_lat, 5),
        "xmax": round(e + pad_lon, 5), "ymax": round(n + pad_lat, 5),
        "spatialReference": {"wkid": 4326},
    })
    data = query_layer(
        url,
        {
            "geometry": envelope,
            "geometryType": "esriGeometryEnvelope",
            "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "*",
            "returnGeometry": "true",
            "outSR": "4326",
        },
        http_request=http_request or _request,
        sleep=sleep,
        retry_delays=config.retry_delays_s,
        cache=cache,
        cache_key=f"roadsgis:{url}:{envelope}",  # url: TX/FL layers never mix
    )
    if "error" in data:
        return None
    # Some servers (TIGERweb) hand geometry back in Web Mercator meters even
    # when outSR=4326 is requested. Coordinates like (-10.6M, 3.5M) parsed as
    # lon/lat put every road on another planet -> every parcel "landlocked".
    # Detect by declared wkid or by magnitude and convert; can't tell -> fail.
    wkid = ((data.get("spatialReference") or {}).get("latestWkid")
            or (data.get("spatialReference") or {}).get("wkid"))
    roads = []
    for feat in data.get("features") or []:
        attrs = feat.get("attributes") or {}
        name = next(
            (str(attrs[f]).strip() for f in name_fields
             if attrs.get(f) and str(attrs[f]).strip()),
            "(unnamed road)",
        )
        for path in (feat.get("geometry") or {}).get("paths") or []:
            coords = [tuple(pt[:2]) for pt in path]
            if len(coords) < 2:
                continue
            x0, y0 = coords[0]
            mercator = wkid in (3857, 102100) or abs(x0) > 360 or abs(y0) > 90
            if mercator:
                # Web Mercator's valid extent is ~±20,037,508 m; anything
                # beyond that is some OTHER projection — refuse to guess,
                # a wrong guess silently landlocks every parcel.
                if any(abs(x) > 20_100_000 or abs(y) > 20_100_000
                       for x, y in coords):
                    return None
                coords = [_mercator_to_lonlat(x, y) for x, y in coords]
            roads.append({"name": name, "coords": coords})
    return roads


def _mercator_to_lonlat(x: float, y: float) -> tuple[float, float]:
    """EPSG:3857 meters -> WGS84 (lon, lat) degrees. Pure math, no deps."""

    import math

    r = 6378137.0
    lon = math.degrees(x / r)
    lat = math.degrees(2.0 * math.atan(math.exp(y / r)) - math.pi / 2.0)
    return lon, lat


def compute_frontage(
    rings_wgs84: list[list[tuple[float, float]]],
    roads: list[dict],
    config: ScreenerConfig = DEFAULT_CONFIG,
) -> dict:
    """PURE. {"frontage": "NONE"|feet, "frontage_street": str|None}.

    frontage_ft ~= length of the PARCEL BOUNDARY that lies within
    ``road_buffer_ft`` of a road centerline. Buffering the road (not the
    parcel) and intersecting with the boundary keeps the measurement close
    to the true shared edge; it slightly overstates on corners, which is
    fine for a screening signal.
    """

    flat = [pt for ring in rings_wgs84 for pt in ring]
    lons = [p[0] for p in flat]
    lats = [p[1] for p in flat]
    lon0, lat0 = (min(lons) + max(lons)) / 2, (min(lats) + max(lats)) / 2

    parcel = _polygon_from_rings(to_local_feet(rings_wgs84, lon0, lat0))
    if parcel is None:
        return {"frontage": "NONE", "frontage_street": None}
    boundary = parcel.boundary

    best_ft, best_name = 0.0, None
    for road in roads:
        if len(road.get("coords") or []) < 2:
            continue
        line = LineString(to_local_feet([road["coords"]], lon0, lat0)[0])
        shared = boundary.intersection(line.buffer(config.road_buffer_ft))
        if shared.is_empty:
            continue
        length = shared.length
        if length > best_ft:
            best_ft, best_name = length, road.get("name")

    if best_ft <= 0:
        return {"frontage": "NONE", "frontage_street": None}
    return {"frontage": round(best_ft), "frontage_street": best_name}
