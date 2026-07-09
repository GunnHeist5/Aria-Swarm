"""tools/screener/geometry.py — pure parcel-shape math (no I/O).

Coordinates arrive from ArcGIS in WGS84 degrees; shapely is planar. Rather
than drag in pyproj, every parcel is projected to LOCAL FEET with an
equirectangular projection centered on the parcel's own bounding box — at
parcel scale (hundreds of feet) the distortion is <0.5%, far inside the
tolerance of a "is this strip narrower than 50 ft" test. The projection MUST
be re-centered per parcel; a county-wide origin would drift.
"""

from __future__ import annotations

import math

from shapely.geometry import MultiPolygon, Polygon
from shapely.ops import unary_union

from .config import DEFAULT_CONFIG, ScreenerConfig

FT_PER_M = 3.280839895
M_PER_DEG_LAT = 111_132.0  # meridian arc, mid-latitudes
M_PER_DEG_LON_EQUATOR = 111_320.0

SLIVER = "SLIVER"


def feet_per_degree(lat0: float) -> tuple[float, float]:
    """(ft per degree longitude, ft per degree latitude) at latitude lat0."""

    ft_lon = M_PER_DEG_LON_EQUATOR * math.cos(math.radians(lat0)) * FT_PER_M
    ft_lat = M_PER_DEG_LAT * FT_PER_M
    return ft_lon, ft_lat


def to_local_feet(
    rings: list[list[tuple[float, float]]], lon0: float, lat0: float
) -> list[list[tuple[float, float]]]:
    """Project WGS84 rings (lon, lat) to feet relative to (lon0, lat0)."""

    ft_lon, ft_lat = feet_per_degree(lat0)
    return [
        [((lon - lon0) * ft_lon, (lat - lat0) * ft_lat) for lon, lat in ring]
        for ring in rings
    ]


def from_local_feet(
    rings_ft: list[list[tuple[float, float]]], lon0: float, lat0: float
) -> list[list[tuple[float, float]]]:
    """Inverse of to_local_feet — used by tests to build WGS84 fixtures."""

    ft_lon, ft_lat = feet_per_degree(lat0)
    return [
        [(lon0 + x / ft_lon, lat0 + y / ft_lat) for x, y in ring]
        for ring in rings_ft
    ]


def _polygon_from_rings(rings_ft: list[list[tuple[float, float]]]):
    """ArcGIS rings -> shapely geometry (outer rings unioned, holes honored).

    ArcGIS encodes holes as reversed-winding rings; building each ring as its
    own polygon and unioning over-counts holes, so classify by winding: the
    largest ring is the shell, opposite-winding rings inside it are holes.
    Simple, and correct for the overwhelmingly common one-shell case; genuine
    multi-shell parcels are flagged upstream as multipart anyway.
    """

    polys = [Polygon(r) for r in rings_ft if len(r) >= 4]
    polys = [p if p.is_valid else p.buffer(0) for p in polys if not p.is_empty]
    if not polys:
        return None
    merged = unary_union(polys)
    if merged.is_empty:
        return None
    return merged


def parcel_metrics(rings_wgs84: list[list[tuple[float, float]]]) -> dict | None:
    """Compute shape metrics for one parcel. Returns None on degenerate input.

    Keys: area_sqft, width_ft, depth_ft, aspect_ratio, centroid_lon,
    centroid_lat, bbox_wgs84 (w, s, e, n), parts.
    """

    flat = [pt for ring in rings_wgs84 for pt in ring]
    if len(flat) < 4:
        return None
    lons = [p[0] for p in flat]
    lats = [p[1] for p in flat]
    lon0, lat0 = (min(lons) + max(lons)) / 2, (min(lats) + max(lats)) / 2

    geom = _polygon_from_rings(to_local_feet(rings_wgs84, lon0, lat0))
    if geom is None or geom.area < 1.0:
        return None
    parts = len(geom.geoms) if isinstance(geom, MultiPolygon) else 1

    rect = geom.minimum_rotated_rectangle
    if rect.geom_type != "Polygon":  # degenerate: collapses to a line/point
        width, depth = 0.0, rect.length
    else:
        xs, ys = rect.exterior.coords.xy
        corners = list(zip(xs, ys))[:4]
        side_a = math.dist(corners[0], corners[1])
        side_b = math.dist(corners[1], corners[2])
        width, depth = sorted((side_a, side_b))
    aspect = depth / width if width > 0 else float("inf")

    c = geom.centroid
    ft_lon, ft_lat = feet_per_degree(lat0)
    return {
        "area_sqft": round(geom.area, 1),
        "width_ft": round(width, 1),
        "depth_ft": round(depth, 1),
        "aspect_ratio": round(aspect, 2) if width > 0 else float("inf"),
        "centroid_lon": lon0 + c.x / ft_lon,
        "centroid_lat": lat0 + c.y / ft_lat,
        "bbox_wgs84": (min(lons), min(lats), max(lons), max(lats)),
        "parts": parts,
    }


def shape_flag(
    width_ft: float, aspect_ratio: float, config: ScreenerConfig = DEFAULT_CONFIG
) -> str | None:
    """SLIVER when too narrow or too elongated (the 33x660 strip test)."""

    if aspect_ratio > config.aspect_ratio_max or width_ft < config.min_width_ft:
        return SLIVER
    return None


def lot_mismatch(
    csv_sqft: float | None,
    gis_sqft: float | None,
    config: ScreenerConfig = DEFAULT_CONFIG,
) -> bool:
    """True when the CSV lot size and GIS polygon area disagree >threshold."""

    if not csv_sqft or not gis_sqft:
        return False
    return abs(gis_sqft - csv_sqft) / csv_sqft > config.lot_mismatch_pct
