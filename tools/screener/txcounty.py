"""tools/screener/txcounty.py — one adapter for ANY Texas county CAD layer.

Texas has no statewide cadastral (Florida does — see putnam.py), so every
county publishes its own appraisal-district parcel service. They are the
same shape though: an ArcGIS FeatureServer/MapServer layer keyed by an
account/APN field with an owner field. So instead of a file per county,
this module is a FACTORY: a county is an entry in
``config.tx_county_gis`` — endpoint, key field, owner fields, APN
normalisation — and ``adapter_for("galveston")`` returns something with
the same four functions cli.py expects of harris/putnam.

Why this matters for the coastal counties: the flood stage only needs a
parcel CENTROID, and FEMA's NFHL is nationwide. Galveston and Brazoria
leads were going out with no flood check at all; with a parcel layer wired
in they get the same A/V-zone screen Harris gets.

ENDPOINTS ARE UNVERIFIED from the dev sandbox (it cannot reach county GIS
hosts). Confirm each on the VPS with::

    python screen.py --check --county galveston

and correct the entry in config.yaml if a host has moved — a config edit,
never a code change.
"""

from __future__ import annotations

import json
import re
import time

from .arcgis import _request, query_layer
from .config import DEFAULT_CONFIG, ScreenerConfig

_NON_ALNUM = re.compile(r"[^0-9A-Za-z]+")


def _spec(county: str, config: ScreenerConfig = DEFAULT_CONFIG) -> dict:
    spec = (config.tx_county_gis or {}).get(county)
    if not spec:
        raise KeyError(
            f"no GIS entry for Texas county {county!r} — add one to "
            "tx_county_gis (url, key_field, owner_fields)")
    return spec


def normalize_apn_for(county: str, apn: str | None,
                      config: ScreenerConfig = DEFAULT_CONFIG) -> str | None:
    """Keep the APN AS EXPORTED ('1647-0005-0043-000').

    Whether a CAD stores punctuation is not knowable without asking it, and
    stripping here is lossy — dashes cannot be put back, so a stripped
    value can never be retried against a layer that keeps them, and every
    lead would come back 'missing'. fetch_parcel tries both forms instead;
    ``strip_apn: true`` in the county's entry forces the stripped form when
    a layer is known to want it.
    """

    if not apn:
        return None
    spec = _spec(county, config)
    value = str(apn).strip()
    if spec.get("strip_apn"):
        value = _NON_ALNUM.sub("", value)
    return value or None


def fetch_parcel_for(county: str, account: str,
                     config: ScreenerConfig = DEFAULT_CONFIG, *,
                     http_request=_request, sleep=time.sleep,
                     cache=None) -> dict:
    """Parcel polygon + owner for one account.

    Tries the stripped account first, then the value as given — the same
    both-ways approach putnam.py needs, because whether a CAD stores
    punctuation is not knowable without asking it.
    """

    spec = _spec(county, config)
    key_field = spec["key_field"]
    owner_fields = tuple(spec.get("owner_fields") or ())
    extra_where = spec.get("where") or ""
    out_fields = ",".join((key_field,) + owner_fields
                          + tuple(spec.get("extra_fields") or ()))

    candidates = [account]
    alt = _NON_ALNUM.sub("", account or "")
    if alt and alt != account:
        candidates.append(alt)

    last_error = None
    for candidate in candidates:
        where = f"{key_field}='{candidate}'"
        if extra_where:
            where = f"{where} AND {extra_where}"
        data = query_layer(
            spec["url"],
            {"where": where, "outFields": out_fields,
             "returnGeometry": "true", "outSR": "4326"},
            http_request=http_request, sleep=sleep,
            retry_delays=config.retry_delays_s, cache=cache,
            cache_key=f"tx:{county}:{candidate}",
        )
        if "error" in data:
            last_error = data["error"]
            continue
        features = data.get("features") or []
        if not features:
            continue
        feat = features[0]
        attrs = feat.get("attributes") or {}
        owner = ""
        for field in owner_fields:
            owner = (attrs.get(field) or "").strip()
            if owner:
                break
        acreage = None
        for field in ("Acreage", "ACREAGE", "GIS_ACRES", "acres"):
            try:
                acreage = float(attrs.get(field))
                break
            except (TypeError, ValueError):
                continue
        return {"rings": [[tuple(pt[:2]) for pt in ring]
                          for ring in (feat.get("geometry") or {}).get("rings")
                          or []],
                "owner": owner, "gis_acreage": acreage}
    return {"error": last_error} if last_error else {"missing": True}


def fetch_adjacent_for(county: str, account: str, rings_wgs84: list,
                       config: ScreenerConfig = DEFAULT_CONFIG, *,
                       http_request=_request, sleep=time.sleep,
                       cache=None) -> list[dict]:
    """Neighbouring owners (assemblage candidates). Advisory: any failure
    returns [] rather than sinking the lead."""

    try:
        spec = _spec(county, config)
    except KeyError:
        return []
    if not rings_wgs84:
        return []
    key_field = spec["key_field"]
    owner_fields = tuple(spec.get("owner_fields") or ())
    geometry = json.dumps({
        "rings": [[list(pt) for pt in ring] for ring in rings_wgs84],
        "spatialReference": {"wkid": 4326},
    })
    data = query_layer(
        spec["url"],
        {"geometry": geometry, "geometryType": "esriGeometryPolygon",
         "spatialRel": "esriSpatialRelIntersects", "inSR": "4326",
         "outFields": ",".join((key_field,) + owner_fields),
         "returnGeometry": "false"},
        http_request=http_request, sleep=sleep,
        retry_delays=config.retry_delays_s, cache=cache,
        cache_key=f"tx:{county}:adj:{account}",
    )
    if "error" in data:
        return []
    out, seen = [], set()
    for feat in data.get("features") or []:
        attrs = feat.get("attributes") or {}
        acct = str(attrs.get(key_field) or "").strip()
        if not acct or acct == account or acct in seen:
            continue
        owner = ""
        for field in owner_fields:
            owner = (attrs.get(field) or "").strip()
            if owner:
                break
        seen.add(acct)
        out.append({"owner": owner, "account": acct})
    return out


def adapter_for(county: str):
    """Return a module-shaped adapter for ``county`` — the same four
    functions cli.py calls on harris/putnam."""

    class _Adapter:
        name = county

        @staticmethod
        def normalize_apn(apn, config=DEFAULT_CONFIG):
            return normalize_apn_for(county, apn, config)

        @staticmethod
        def fetch_parcel(account, config=DEFAULT_CONFIG, **kw):
            return fetch_parcel_for(county, account, config, **kw)

        @staticmethod
        def fetch_adjacent(account, rings, config=DEFAULT_CONFIG, **kw):
            return fetch_adjacent_for(county, account, rings, config, **kw)

        @staticmethod
        def roads_config(config=DEFAULT_CONFIG):
            # TIGER first (real street names), TxDOT second — same order and
            # reasoning as the Harris adapter.
            return ((config.tiger_roads_url, config.tiger_roads_name_fields),
                    (config.roads_arcgis_url, config.roads_name_fields))

    return _Adapter
