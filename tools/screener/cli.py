"""tools/screener/cli.py — the Dirt Screener orchestrator.

    python screen.py leads.csv --county harris [--maps] [--limit 20]
                     [--config config.yaml] [--out out/] [--no-resume]
    python screen.py --check          # live endpoint probes (run on the VPS)

Stage order is the cost gradient: parse (free) -> geometry -> frontage
(cheap public GIS) -> flood (one free call) -> comps (search quota + LLM
tokens, survivors only) -> score (pure). Every enrichment stage's result is
persisted per (account, stage, genome-hash), so an interrupted run resumes
where it stopped and a re-run after a config tweak only re-derives what the
tweak invalidated.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(override=True)
except ImportError:
    pass

from . import fema, frontage, harris
from .cache import Cache
from .comps import BraveAuthError, BraveClient, run_comps
from .config import ScreenerConfig, config_hash, load_config
from .geometry import lot_mismatch, parcel_metrics, shape_flag
from .scoring import score_row

COUNTY_ADAPTERS = {"harris": harris}

ENRICH_STAGES = ("geometry", "frontage", "flood", "comps")


def _parse_stage(row: dict, config: ScreenerConfig, county) -> None:
    """Stage 1: normalize APN, coerce lot size / asking, flag needs_manual."""

    row.setdefault("needs_manual_reason", None)
    account = county.normalize_apn(row.get("APN"))
    row["hcad_account"] = account
    if not account:
        row["needs_manual_reason"] = (
            "missing APN" if not (row.get("APN") or "").strip() else "bad APN format"
        )

    try:
        lot = float(str(row.get("Lot Size Sqft") or "").replace(",", ""))
        row["_lot_sqft"] = lot if lot > 0 else None
    except ValueError:
        row["_lot_sqft"] = None
    if row["_lot_sqft"] is None and not row["needs_manual_reason"]:
        row["needs_manual_reason"] = "missing lot size"

    row["_asking"] = None
    for col in config.asking_columns:
        try:
            row["_asking"] = float(str(row.get(col) or "").replace(",", "").replace("$", ""))
            break
        except ValueError:
            continue


def _seam(http_request) -> dict:
    """kwargs for an adapter call: inject the stub, or let its default run."""

    return {"http_request": http_request} if http_request else {}


def _geometry_stage(row: dict, config: ScreenerConfig, county, *,
                    http_request=None, sleep, cache) -> dict:
    parcel = county.fetch_parcel(
        row["hcad_account"], config,
        **_seam(http_request), sleep=sleep, cache=cache)
    if parcel.get("error"):
        return {"needs_manual_reason": f"parcel fetch failed: {parcel['error'][:120]}"}
    if parcel.get("missing"):
        return {"needs_manual_reason": "parcel not found in HCAD"}

    metrics = parcel_metrics(parcel["rings"])
    if metrics is None:
        return {"needs_manual_reason": "degenerate parcel geometry"}
    if metrics["parts"] > 1:
        return {"needs_manual_reason": "multipart parcel"}

    adjacent = county.fetch_adjacent(
        row["hcad_account"], parcel["rings"], config,
        **_seam(http_request), sleep=sleep, cache=cache)
    return {
        "width_ft": metrics["width_ft"],
        "depth_ft": metrics["depth_ft"],
        "aspect_ratio": metrics["aspect_ratio"],
        "shape_flag": shape_flag(metrics["width_ft"], metrics["aspect_ratio"], config),
        "adjacent_owners": "; ".join(
            f"{a['owner']} ({a['account']})" for a in adjacent if a.get("owner")
        ),
        "_adjacent": adjacent,
        "_rings": parcel["rings"],
        "_centroid": (metrics["centroid_lat"], metrics["centroid_lon"]),
        "_bbox": metrics["bbox_wgs84"],
        "_lot_mismatch": lot_mismatch(row.get("_lot_sqft"), metrics["area_sqft"], config),
    }


def _frontage_stage(row: dict, config: ScreenerConfig, *, http_request=None,
                    sleep, cache) -> dict:
    roads = frontage.fetch_roads(
        row["_bbox"], config, sleep=sleep, cache=cache, **_seam(http_request))
    if roads is None:  # fetch failure must never look landlocked
        return {"needs_manual_reason": "road data unavailable"}
    result = frontage.compute_frontage(row["_rings"], roads, config)
    result["_roads"] = roads
    return result


def _flood_stage(row: dict, config: ScreenerConfig, *, http_request=None,
                 sleep, cache) -> dict:
    lat, lon = row["_centroid"]
    result = fema.flood_zone(
        lat, lon, config, **_seam(http_request), sleep=sleep, cache=cache)
    if result.get("error"):
        # Flood is a soft signal — an outage shouldn't park the lead.
        return {"flood_zone": "UNKNOWN", "flood_flag": None}
    return result


def run_pipeline(
    rows: list[dict],
    config: ScreenerConfig,
    *,
    county=harris,
    http_request=None,       # None => each adapter's real default
    overpass_request=None,
    sleep=time.sleep,
    llm=None,
    brave: BraveClient | None = None,
    cache: Cache | None = None,
    limit: int | None = None,
    resume: bool = True,
    log=print,
) -> list[dict]:
    """Run stages 1-6 over the rows in place; returns the same list."""

    cfg_hash = config_hash(config)
    comps_enabled = brave is not None and llm is not None
    comps_dead = False
    road_failures = 0  # consecutive; 3 trips the breaker for this run

    def cached_stage(row, stage, fn) -> dict:
        account = row["hcad_account"]
        if resume and cache is not None:
            hit = cache.get_stage(account, stage, cfg_hash)
            if hit is not None:
                return hit
        payload = fn()
        # Failures are never cached — a rerun must retry them, not resume them.
        if cache is not None and not payload.get("needs_manual_reason"):
            cache.put_stage(account, stage, cfg_hash, _json_safe(payload))
        return payload

    processed = 0
    for row in rows:
        if limit is not None and processed >= limit:
            break
        _parse_stage(row, config, county)
        stage_reached = "parse"

        if not row["needs_manual_reason"]:
            processed += 1
            merged = cached_stage(row, "geometry", lambda: _geometry_stage(
                row, config, county, http_request=http_request,
                sleep=sleep, cache=cache))
            row.update(merged)
            stage_reached = "geometry"

        if not row["needs_manual_reason"] and row.get("_rings"):
            if road_failures >= 3:
                # Overpass is down for this run — fail fast instead of
                # burning the whole retry ladder on every remaining lead.
                # Not cached, so the next run retries all of them.
                row["needs_manual_reason"] = "road data unavailable"
            else:
                payload = cached_stage(row, "frontage", lambda: _frontage_stage(
                    row, config, http_request=overpass_request, sleep=sleep,
                    cache=cache))
                row.update(payload)
                if payload.get("needs_manual_reason") == "road data unavailable":
                    road_failures += 1
                else:
                    road_failures = 0
            stage_reached = "frontage"

        if not row["needs_manual_reason"] and row.get("_centroid"):
            row.update(cached_stage(row, "flood", lambda: _flood_stage(
                row, config, http_request=http_request, sleep=sleep,
                cache=cache)))
            stage_reached = "flood"

        killed = row.get("shape_flag") or row.get("frontage") == "NONE"
        if (
            not row["needs_manual_reason"] and not killed
            and comps_enabled and not comps_dead and row.get("_lot_sqft")
        ):
            try:
                row.update(cached_stage(row, "comps", lambda: run_comps(
                    row, config, brave=brave, llm=llm)))
                stage_reached = "comps"
                sleep(60.0 / config.comps_leads_per_min)
            except BraveAuthError as exc:
                comps_dead = True
                log(f"!! comps aborted (auth): {exc}")
        elif not killed and not row["needs_manual_reason"] and not comps_enabled:
            row.setdefault("comp_evidence", "comps skipped: no API key/LLM")

        row.update(score_row(row, config))
        if row.get("_lot_mismatch") and not row["needs_manual_reason"]:
            note = "GIS area vs CSV lot size mismatch >15%"
            prior = row.get("comp_evidence") or ""
            row["comp_evidence"] = f"{prior} | {note}" if prior else note

        label = row.get("verdict") or f"needs_manual ({row['needs_manual_reason']})"
        extra = f" ({row['shape_flag']})" if row.get("shape_flag") else ""
        log(f"{row.get('hcad_account') or row.get('APN') or '?'} "
            f"stage={stage_reached} verdict={label}{extra}")
    return rows


def _json_safe(payload: dict) -> dict:
    """Stage payloads must round-trip JSON for the cache (tuples -> lists)."""

    return json.loads(json.dumps(payload, default=list))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Dirt Screener — vacant-land lead enrichment pipeline.")
    parser.add_argument("leads", nargs="?", help="PropStream export (.csv/.xlsx)")
    parser.add_argument("--county", default="harris", choices=sorted(COUNTY_ADAPTERS))
    parser.add_argument("--maps", action="store_true",
                        help="write an SVG parcel sketch per non-PASS lead")
    parser.add_argument("--limit", type=int, default=None,
                        help="process at most N rows (sample runs)")
    parser.add_argument("--config", default=None, help="YAML config overlay")
    parser.add_argument("--out", default="out", help="output directory")
    parser.add_argument("--no-resume", action="store_true",
                        help="ignore cached stage results (HTTP cache still used)")
    parser.add_argument("--check", action="store_true",
                        help="live-probe HCAD/FEMA/Overpass/Brave endpoints and exit")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    if args.county != config.county:
        config = config.mutate(county=args.county)

    if args.check:
        from .check import run_check

        return run_check(config)

    if not args.leads:
        parser.error("leads file required (or use --check)")

    from tools.integrations.leadfile import parse_propstream

    rows = parse_propstream(args.leads)
    if not rows:
        print("no rows in export", file=sys.stderr)
        return 1

    cache = Cache()
    county = COUNTY_ADAPTERS[config.county]

    # Stage-5 dependencies are optional: without a Brave key or Anthropic key
    # the pipeline still runs the deterministic stages and says so per row.
    brave = llm = None
    try:
        from tools.integrations.secrets import get_secret

        brave_key = get_secret("BRAVE_API_KEY") or get_secret("SEARCH_API_KEY")
        if brave_key:
            brave = BraveClient(brave_key, config, cache=cache)
        import os

        if os.environ.get("ANTHROPIC_API_KEY"):
            from langchain_anthropic import ChatAnthropic

            llm = ChatAnthropic(
                model=os.environ.get(config.comps_model_env, "claude-sonnet-4-6"),
                temperature=0.0,
            )
    except Exception as exc:  # comps are optional; screening is not
        print(f"comps disabled: {exc}", file=sys.stderr)

    run_pipeline(
        rows, config, county=county, llm=llm, brave=brave, cache=cache,
        limit=args.limit, resume=not args.no_resume)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    from .output import write_summary, write_xlsx

    input_headers = [k for k in rows[0].keys() if not k.startswith("_")
                     and k not in ("hcad_account", "needs_manual_reason")]
    write_xlsx(rows, input_headers, out_dir / "enriched_leads.xlsx")
    write_summary(rows, out_dir / "summary.md")

    if args.maps:
        from .maps import render_svg

        maps_dir = out_dir / "maps"
        maps_dir.mkdir(exist_ok=True)
        for row in rows:
            if row.get("verdict") not in ("", "PASS") and row.get("_rings"):
                render_svg(row["_rings"], row.get("_roads"), row,
                           maps_dir / f"{row['hcad_account']}.svg")

    print(f"\nwrote {out_dir}/enriched_leads.xlsx and {out_dir}/summary.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
