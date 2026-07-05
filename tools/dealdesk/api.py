"""tools/dealdesk/api.py — the HTTP endpoint the voice agent (Trillet) calls.

  POST /offer-range   {address?, apn?, owner_name?}  (Bearer auth)
      -> {found, opening_offer, max_offer, escalate, escalate_reason, property, notes}
  GET  /health

Run on the VPS:
    DEALDESK_API_KEY=... DEALDESK_EXPORT_PATH=/root/.hermes/exports/land.xlsx \
      /root/Aria-Swarm/.venv/bin/uvicorn tools.dealdesk.api:app --host 127.0.0.1 --port 8088

Security posture:
  * Bearer auth is REQUIRED. With ``DEALDESK_API_KEY`` unset the endpoint refuses
    every request (fail-closed — never run an unauthenticated pricing endpoint).
  * The lookup source loads lazily; if the export path is missing/unreadable,
    every lookup returns escalate=not_found so the agent hands off to a human
    rather than pricing on stale/empty data.
  * Responses never include ARV / formula / margin — only the band.
"""

from __future__ import annotations

import os

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from .lookup import FileLookup
from .pricing import compute_offer_range

app = FastAPI(title="ARIA Capital deal desk", version="1.0")

_LOOKUP = None
_LOOKUP_ERR = None


def _lookup():
    """Lazily build the property index from DEALDESK_EXPORT_PATH (cached)."""

    global _LOOKUP, _LOOKUP_ERR
    if _LOOKUP is not None or _LOOKUP_ERR is not None:
        return _LOOKUP
    path = os.environ.get("DEALDESK_EXPORT_PATH")
    if not path:
        _LOOKUP_ERR = "DEALDESK_EXPORT_PATH not set"
        return None
    try:
        _LOOKUP = FileLookup(path)
    except Exception as exc:  # missing/corrupt export -> escalate, don't crash
        _LOOKUP_ERR = f"lookup load failed: {exc}"
    return _LOOKUP


def require_auth(authorization: str = Header(default="")) -> None:
    """Bearer-token gate. Fail-closed when no key is configured."""

    key = os.environ.get("DEALDESK_API_KEY")
    if not key:
        raise HTTPException(status_code=503, detail="deal desk not configured")
    if authorization != f"Bearer {key}":
        raise HTTPException(status_code=401, detail="unauthorized")


class OfferQuery(BaseModel):
    address: str | None = None
    apn: str | None = None
    owner_name: str | None = None


@app.get("/health")
def health() -> dict:
    lk = _lookup()
    return {
        "ok": True,
        "lookup_loaded": lk is not None,
        "lookup_error": _LOOKUP_ERR,
        "properties": (len(lk.by_addr) if lk else 0),
    }


@app.post("/offer-range")
def offer_range(q: OfferQuery, _: None = Depends(require_auth)) -> dict:
    """Look up a property and return its negotiation band (or escalate)."""

    if not (q.address or q.apn):
        raise HTTPException(status_code=422, detail="address or apn required")
    lk = _lookup()
    if lk is None:
        # No data source -> safe degrade: tell the agent to escalate.
        return {
            "found": False, "opening_offer": None, "max_offer": None,
            "escalate": True, "escalate_reason": "data_unavailable",
            "property": None, "notes": _LOOKUP_ERR or "lookup unavailable",
        }
    record = lk.find(address=q.address, apn=q.apn, owner_name=q.owner_name)
    return compute_offer_range(record)
