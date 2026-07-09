"""tools/screener/maps.py — dependency-free SVG parcel sketches (--maps).

The brief asked for "eyeball the shape in 2 seconds" imagery. Instead of
folium + tile fetching (network, deps, slow), each non-PASS lead gets a small
SVG: the parcel outline in its verdict color, nearby road centerlines with
street names, and a width x depth caption. Opens instantly in any browser,
renders offline, and diffs cleanly in git.
"""

from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape

from .geometry import to_local_feet
from .output import VERDICT_FILLS

_SIZE = 400
_PAD = 30


def _project_all(rings_wgs84, roads, lon0, lat0):
    parcel_ft = to_local_feet(rings_wgs84, lon0, lat0)
    roads_ft = [
        dict(road, coords_ft=to_local_feet([road["coords"]], lon0, lat0)[0])
        for road in roads or []
        if len(road.get("coords") or []) >= 2
    ]
    return parcel_ft, roads_ft


def render_svg(
    rings_wgs84: list[list[tuple[float, float]]],
    roads: list[dict] | None,
    row: dict,
    path: str | Path,
) -> None:
    flat = [pt for ring in rings_wgs84 for pt in ring]
    if len(flat) < 3:
        return
    lons = [p[0] for p in flat]
    lats = [p[1] for p in flat]
    lon0, lat0 = (min(lons) + max(lons)) / 2, (min(lats) + max(lats)) / 2
    parcel_ft, roads_ft = _project_all(rings_wgs84, roads, lon0, lat0)

    pts = [pt for ring in parcel_ft for pt in ring]
    pts += [pt for road in roads_ft for pt in road["coords_ft"]]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    span = max(max(xs) - min(xs), max(ys) - min(ys)) or 1.0
    scale = (_SIZE - 2 * _PAD) / span

    def sx(x: float) -> float:
        return _PAD + (x - min(xs)) * scale

    def sy(y: float) -> float:
        return _SIZE - _PAD - (y - min(ys)) * scale  # north up

    def path_d(coords) -> str:
        return " ".join(
            f"{'M' if i == 0 else 'L'}{sx(x):.1f},{sy(y):.1f}"
            for i, (x, y) in enumerate(coords)
        )

    color = "#" + VERDICT_FILLS.get(row.get("verdict") or "", "CCCCCC")
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_SIZE}" '
        f'height="{_SIZE}" viewBox="0 0 {_SIZE} {_SIZE}" '
        f'style="background:#ffffff;font-family:sans-serif">'
    ]
    for road in roads_ft:
        parts.append(
            f'<path d="{path_d(road["coords_ft"])}" stroke="#999999" '
            f'stroke-width="3" fill="none"/>'
        )
        mx, my = road["coords_ft"][len(road["coords_ft"]) // 2]
        parts.append(
            f'<text x="{sx(mx):.0f}" y="{sy(my) - 4:.0f}" font-size="9" '
            f'fill="#666666">{escape(road.get("name") or "")}</text>'
        )
    for ring in parcel_ft:
        parts.append(
            f'<path d="{path_d(ring)} Z" stroke="#333333" stroke-width="1.5" '
            f'fill="{color}" fill-opacity="0.65"/>'
        )
    caption = (
        f'{row.get("Address") or row.get("hcad_account") or "?"} — '
        f'{row.get("width_ft", "?")}×{row.get("depth_ft", "?")} ft, '
        f'AR {row.get("aspect_ratio", "?")} — {row.get("verdict") or "?"}'
    )
    parts.append(
        f'<text x="{_PAD}" y="{_SIZE - 8}" font-size="11" fill="#222222">'
        f"{escape(caption)}</text>"
    )
    parts.append("</svg>")
    Path(path).write_text("".join(parts), encoding="utf-8")
