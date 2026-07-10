"""tools/screener/config.py — the Dirt Screener genome.

Every decision number and endpoint URL lives here as data (frozen dataclass,
same pattern as tools/wholesaling/config.py), overridable from a YAML file via
``load_config`` so field ops can tune thresholds without touching code. The
endpoint URLs are config on purpose — GIS servers move; the brief requires the
base URLs to be data, not code.

``config_hash`` fingerprints the genome for the stage-result cache: change a
threshold and cached stage results derived under the old genome are ignored.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace


@dataclass(frozen=True)
class ScreenerConfig:
    # -- kill thresholds (stage 2/3) --
    aspect_ratio_max: float = 4.0     # min-rotated-rect long/short above this => SLIVER
    min_width_ft: float = 50.0        # short side below this => SLIVER
    lot_mismatch_pct: float = 0.15    # GIS area vs CSV lot size disagreement flag
    road_buffer_ft: float = 5.0       # parcel buffer for the frontage intersection test

    # -- valuation / fees (stage 6) --
    buyer_ceiling_ratio: float = 0.75  # retail * this = investor buyer ceiling (0.70-0.80)
    target_fee_usd: float = 10_000.0
    min_fee_usd: float = 5_000.0
    flood_cut_pct: float = 0.30        # suggested renegotiation cut for floodplain
    open_at_ratio: float = 0.85        # opening offer = mao * this
    small_lot_sqft: float = 10_000.0   # $/sqft regime split: infill vs acreage
    ppsf_min: float = 0.10             # sanity clamp on LLM-extracted $/sqft
    ppsf_max: float = 100.0

    # -- throttles / limits --
    comps_leads_per_min: float = 6.0   # stage-5 pace (brief: max N leads/min)
    brave_query_spacing_s: float = 1.1  # Brave free tier is 1 req/s
    brave_results_count: int = 10
    max_auth_failures: int = 3         # consecutive 401/403 => abort comps, fail closed
    retry_delays_s: tuple = (2.0, 10.0, 30.0)  # 429/5xx backoff ladder
    request_timeout_s: float = 30.0

    # -- endpoints (data, not code — servers move) --
    county: str = "harris"
    hcad_parcels_url: str = (
        "https://www.gis.hctx.net/arcgis/rest/services/HCAD/Parcels/MapServer/0"
    )
    hcad_account_field: str = "HCAD_NUM"       # 13-digit account column on the layer
    hcad_owner_fields: tuple = ("owner_name_1", "owner_name_2", "owner_name_3")
    fema_nfhl_url: str = (
        "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28"
    )
    fema_nfhl_fallback_url: str = (
        "https://hazards.fema.gov/gis/nfhl/rest/services/public/NFHL/MapServer/28"
    )
    # Esri Living Atlas mirror of the NFHL (S_Fld_Haz_Ar, reduced set) — on
    # Esri infrastructure, so it works when FEMA's WAF blocks the VPS's IP or
    # TLS fingerprint outright. Slightly staler than FEMA's live layer;
    # acceptable for a soft-kill negotiation signal.
    fema_agol_fallback_url: str = (
        "https://services.arcgis.com/P3ePLMYs2RVChkJx/arcgis/rest/services/"
        "USA_Flood_Hazard_Reduced_Set_gdb/FeatureServer/0"
    )
    # Overpass mirrors in preference order — the ecosystem rate-limits
    # pipelines aggressively, so spread across mirrors and stay polite.
    overpass_urls: tuple = (
        "https://overpass.kumi.systems/api/interpreter",
        "https://lz4.overpass-api.de/api/interpreter",
        "https://overpass-api.de/api/interpreter",
    )
    overpass_spacing_s: float = 1.0    # courtesy gap before each live fetch
    brave_url: str = "https://api.search.brave.com/res/v1/web/search"

    # -- input mapping --
    asking_columns: tuple = ("Listing Amount", "MLS Amount", "Asking Price")

    # -- LLM --
    comps_model_env: str = "CLAUDE_MODEL"  # swap to CLAUDE_CHEAP_MODEL to cut comps cost

    def mutate(self, **changes) -> "ScreenerConfig":
        return replace(self, **changes)


DEFAULT_CONFIG = ScreenerConfig()


def load_config(path: str | None = None) -> ScreenerConfig:
    """Default genome, overlaid with a YAML file when one is given.

    Unknown keys are an error — a typo'd threshold must not silently leave the
    default in force.
    """

    if not path:
        return DEFAULT_CONFIG
    import yaml

    with open(path, encoding="utf-8") as fh:
        overrides = yaml.safe_load(fh) or {}
    if not isinstance(overrides, dict):
        raise ValueError(f"config file must be a YAML mapping: {path}")
    known = set(asdict(DEFAULT_CONFIG))
    unknown = set(overrides) - known
    if unknown:
        raise ValueError(f"unknown config keys: {sorted(unknown)}")
    # YAML lists arrive as lists; frozen dataclass fields expect tuples.
    coerced = {
        k: tuple(v) if isinstance(getattr(DEFAULT_CONFIG, k), tuple) else v
        for k, v in overrides.items()
    }
    return DEFAULT_CONFIG.mutate(**coerced)


def config_hash(config: ScreenerConfig = DEFAULT_CONFIG) -> str:
    payload = json.dumps(asdict(config), sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
