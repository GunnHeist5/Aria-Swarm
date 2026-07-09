"""tools/screener — the Dirt Screener: vacant-land lead enrichment pipeline.

Takes a PropStream export (CSV/XLSX), enriches every row with the checks a
human would do by hand — parcel geometry from HCAD (kill slivers), road
frontage from OSM (kill landlocked), FEMA flood zone (soft kill / negotiation
lever), then web comps + LLM extraction for the survivors — and emits a scored
XLSX + summary.md with a verdict and max allowable offer per lead.

Design split:
  * pure decision logic (geometry.py, scoring.py, fema.classify_zone,
    frontage.compute_frontage, comps.snippet_quality) — no I/O, fully
    offline-tested;
  * network adapters (arcgis.py, harris.py, fema.py, frontage.fetch_roads,
    comps.brave_search) — injectable ``http_request``/``sleep`` seams;
  * cache.py — SQLite response cache + per-stage resumability store;
  * cli.py — the orchestrator; ``python screen.py leads.csv --county harris``.

Cheap deterministic kills run first; API tokens are only spent on rows that
survive stages 2-4. Kill flags fail closed: a fetch failure routes a lead to
needs_manual, never to a kill verdict.
"""

from .config import DEFAULT_CONFIG, ScreenerConfig, config_hash, load_config
from .scoring import score_row
from .geometry import parcel_metrics, shape_flag

__all__ = [
    "DEFAULT_CONFIG",
    "ScreenerConfig",
    "config_hash",
    "load_config",
    "score_row",
    "parcel_metrics",
    "shape_flag",
]
