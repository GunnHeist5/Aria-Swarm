"""tools/screener/arcgis.py — shared ArcGIS REST query helper.

One function every ArcGIS-backed adapter (HCAD parcels, FEMA NFHL) goes
through: URL-encoded GET/POST against a layer's /query endpoint with retry
on transient failures and transparent pagination when the server truncates
at maxRecordCount (exceededTransferLimit).

Network is a single injectable ``http_request`` (contracts.py convention);
the default sends a browser User-Agent because ArcGIS front-doors (and the
Cloudflare in front of some of them) 403 python's default UA.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

RETRYABLE = {429, 500, 502, 503, 504}


def _request(method: str, url: str, payload: dict | None, key: str) -> tuple[int, str]:
    """(status, body) — payload is form-encoded for POST, ignored for GET."""

    data = urllib.parse.urlencode(payload).encode("ascii") if payload else None
    req = urllib.request.Request(
        url,
        data=data if method == "POST" else None,
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method=method,
    )
    return open_with_tls_fallback(req, timeout=30)


def open_with_tls_fallback(req: urllib.request.Request, *, timeout: float) -> tuple[int, str]:
    """urlopen, retrying SSL failures with a TLS 1.2-pinned context.

    Some federal WAFs (FEMA's included) drop Python's default TLS 1.3
    handshake mid-stream ("UNEXPECTED_EOF_WHILE_READING") while accepting a
    plain TLS 1.2 one — observed live on hazards.fema.gov from the VPS.
    """

    import ssl

    contexts: tuple = (None,)
    try:
        legacy = ssl.create_default_context()
        legacy.maximum_version = ssl.TLSVersion.TLSv1_2
        contexts = (None, legacy)
    except (AttributeError, ssl.SSLError):
        pass

    last: tuple[int, str] = (0, "network error: no attempt made")
    for ctx in contexts:
        try:
            kwargs = {"timeout": timeout}
            if ctx is not None:
                kwargs["context"] = ctx
            with urllib.request.urlopen(req, **kwargs) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")
        except urllib.error.URLError as exc:
            last = (0, f"network error: {exc}")
            if not isinstance(exc.reason, ssl.SSLError):
                return last  # not TLS — a downgraded retry won't help
        except (ssl.SSLError,) as exc:
            last = (0, f"network error: {exc}")
        except (TimeoutError, OSError) as exc:
            return 0, f"network error: {exc}"
    return last


def query_layer(
    base_url: str,
    params: dict,
    *,
    http_request=_request,
    sleep=time.sleep,
    retry_delays=(2.0, 10.0, 30.0),
    cache=None,
    cache_key: str | None = None,
) -> dict:
    """Query an ArcGIS layer, following pagination. Returns the merged JSON.

    On success: ``{"features": [...], ...}``. On failure after retries:
    ``{"error": "..."}`` — callers route the lead to needs_manual, they do
    not treat a fetch failure as a screening verdict (fail closed).
    """

    if cache is not None and cache_key:
        hit = cache.get_http(cache_key)
        if hit is not None:
            return json.loads(hit)

    merged: dict = {}
    features: list = []
    offset = 0
    while True:
        page = dict(params, f="json")
        if offset:
            page["resultOffset"] = offset
        url = f"{base_url}/query?{urllib.parse.urlencode(page)}"

        status, body = 0, ""
        for attempt, delay in enumerate((0.0,) + tuple(retry_delays)):
            if delay:
                sleep(delay)
            status, body = http_request("GET", url, None, "")
            if status == 200:
                break
            if status not in RETRYABLE and status != 0:
                break
        if status != 200:
            return {"error": f"HTTP {status}: {body[:200]}"}

        try:
            data = json.loads(body)
        except ValueError:
            return {"error": f"unparseable response: {body[:200]}"}
        if "error" in data:  # ArcGIS returns errors inside a 200
            return {"error": json.dumps(data["error"])[:300]}

        merged = data
        features.extend(data.get("features") or [])
        if not data.get("exceededTransferLimit"):
            break
        offset = len(features)

    merged["features"] = features
    if cache is not None and cache_key:
        cache.put_http(cache_key, 200, json.dumps(merged))
    return merged
