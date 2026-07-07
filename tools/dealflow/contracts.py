"""tools/dealflow/contracts.py — PandaDoc purchase/assignment agreement send.

Ports Muffin's proven integration (``/root/.hermes/muffin_pandadoc_contract.py``):
same template, same field names, same create→send flow — so it works against the
real PandaDoc template with zero guesswork. The only change is *where* it runs:
inside the swarm (tested, one system) instead of a dormant Muffin script.

Credentials, env-first with a fallback to Muffin's vault so nothing needs
re-gathering:
  * API key: ``PANDADOC_API_KEY`` → else ``~/.hermes/vault.json`` pandadoc key.
  * Template: ``PANDADOC_PURCHASE_TEMPLATE_ID`` → else Muffin's known template.

⚠️ That vault key was exposed earlier and is COMPROMISED — rotate it in PandaDoc
and put the fresh key in ``.env`` as ``PANDADOC_API_KEY``; the vault fallback is
only a stopgap so this works today.

All network is a single injectable ``http_request`` for offline testing.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

_BASE = "https://api.pandadoc.com/public/v1"
_VAULT = Path(os.environ.get("HERMES_VAULT", os.path.expanduser("~/.hermes/vault.json")))
# Muffin's proven purchase/assignment template (override via env).
_DEFAULT_TEMPLATE = "Gqk4KM3eVUAth5ABtxK5Sj"

_STANDARD_TERMS = (
    "This agreement is subject to partner approval within 5 business days. "
    "Seller acknowledges buyer is purchasing for investment purposes and may "
    "market the property prior to closing."
)


def resolve_api_key() -> str:
    key = os.environ.get("PANDADOC_API_KEY")
    if key:
        return key
    try:
        return json.loads(_VAULT.read_text()).get("pandadoc", {}).get("production_api_key", "")
    except Exception:
        return ""


def resolve_template_id() -> str:
    return os.environ.get("PANDADOC_PURCHASE_TEMPLATE_ID") or _DEFAULT_TEMPLATE


def _request(method: str, url: str, payload: dict | None, key: str) -> tuple[int, str]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        headers={"Authorization": f"API-Key {key}", "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def build_fields(deal: dict) -> dict:
    """Map a deal dict to Muffin's proven PandaDoc field names."""

    return {
        "property_address": deal.get("property_address") or deal.get("address", ""),
        "property_city": deal.get("city", ""),
        "property_state": deal.get("state", ""),
        "property_zip": deal.get("zip", ""),
        "seller_name": str(deal.get("seller_name", "")),
        "seller_email": deal.get("contact_email") or deal.get("contact", ""),
        "purchase_price": str(deal.get("agreed_price") or deal.get("offer_price", "")),
        "assignment_fee": str(deal.get("assignment_fee", "")),
        "closing_date": deal.get("closing_date", ""),
        "buyer_name": "ARIA Capital LLC",
        "buyer_email": "justin@ariacapital.tech",
        "special_terms": _STANDARD_TERMS,
    }


def create_and_send(deal: dict, *, api_key: str, template_id: str,
                    http_request=_request) -> dict:
    """Create the document from the template and send it for e-sign.

    Mirrors Muffin's create→send (``template_id`` + ``fields`` payload). Returns
    ``{ok, document_id, status_code, detail}``.
    """

    fields = build_fields(deal)
    payload = {
        "name": f"Purchase Agreement — {fields['property_address'] or 'Property'}",
        "template_id": template_id,
        "recipients": [{
            "email": fields["seller_email"],
            "first_name": (fields["seller_name"].split() or [""])[0],
            "last_name": " ".join(fields["seller_name"].split()[1:]),
            "role": "Seller",
        }],
        "fields": fields,
        "options": {"send_completed_email": False},
        "metadata": {"deal_id": deal.get("deal_id")},
    }
    status, resp = http_request("POST", f"{_BASE}/documents", payload, api_key)
    if status != 201:
        return {"ok": False, "document_id": None, "status_code": status, "detail": resp[:300]}

    doc_id = json.loads(resp or "{}").get("id")
    s2, r2 = http_request(
        "POST", f"{_BASE}/documents/{doc_id}/send",
        {"message": "Please review and sign your cash purchase agreement.", "silent": False},
        api_key,
    )
    return {"ok": 200 <= s2 < 300, "document_id": doc_id, "status_code": s2, "detail": r2[:300]}
