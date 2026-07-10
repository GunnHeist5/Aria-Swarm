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


def compute_frontage(
    rings_wgs84: list[list[tuple[float, float]]],
    roads: list[dict],
    config: ScreenerConfig = DEFAULT_CONFIG,
) -> dict:
    """PURE. {"frontage": "NONE"|feet, "frontage_street": str|None}.

    frontage_ft = length of the road centerline crossing the parcel's
    ~5 ft buffer — a proxy for the shared edge with the right-of-way.
    """

    flat = [pt for ring in rings_wgs84 for pt in ring]
    lons = [p[0] for p in flat]
    lats = [p[1] for p in flat]
    lon0, lat0 = (min(lons) + max(lons)) / 2, (min(lats) + max(lats)) / 2

    parcel = _polygon_from_rings(to_local_feet(rings_wgs84, lon0, lat0))
    if parcel is None:
        return {"frontage": "NONE", "frontage_street": None}
    buffered = parcel.buffer(config.road_buffer_ft)

    best_ft, best_name = 0.0, None
    for road in roads:
        if len(road.get("coords") or []) < 2:
            continue
        line = LineString(to_local_feet([road["coords"]], lon0, lat0)[0])
        shared = line.intersection(buffered)
        if shared.is_empty:
            continue
        length = shared.length
        if length > best_ft:
            best_ft, best_name = length, road.get("name")

    if best_ft <= 0:
        return {"frontage": "NONE", "frontage_street": None}
    return {"frontage": round(best_ft), "frontage_street": best_name}
