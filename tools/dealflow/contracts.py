"""tools/dealflow/contracts.py — PandaDoc: create a purchase agreement and e-sign it.

On Accept, create a document from the purchase-agreement template with the deal's
fields and send it to the seller for signature (they get an email with a sign
link — exactly what a seller is comfortable with, and legally binding). PandaDoc's
webhook later reports the signature back (handled in ``router.py``).

Network is a single injectable ``http_request`` for offline testing. Live creds:
``PANDADOC_API_KEY`` + ``PANDADOC_PURCHASE_TEMPLATE_ID``.

NOTE: field/token/role names below must match your actual PandaDoc template — tune
them on the VPS against the real template (this is the seam, verified live like
the other integrations).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

_BASE = "https://api.pandadoc.com/public/v1"


def _request(method: str, url: str, payload: dict | None, api_key: str) -> tuple[int, str]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        headers={"Authorization": f"API-Key {api_key}", "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def _split_name(deal: dict) -> tuple[str, str]:
    first = deal.get("seller_first")
    last = deal.get("seller_last")
    if first or last:
        return first or "", last or ""
    parts = str(deal.get("seller_name", "")).split()
    return (parts[0] if parts else ""), (" ".join(parts[1:]) if len(parts) > 1 else "")


def create_and_send(deal: dict, *, api_key: str, template_id: str,
                    http_request=_request, sleep=time.sleep,
                    draft_poll: int = 6) -> dict:
    """Create the doc from the template, wait for draft, and send it for e-sign.

    Returns ``{ok, document_id, status_code, detail}``. PandaDoc creates in an
    ``uploaded`` state and must reach ``draft`` before it can be sent, so we poll
    the document status briefly before sending.
    """

    first, last = _split_name(deal)
    seller_email = deal.get("contact_email") or deal.get("contact")
    body = {
        "name": f"Purchase Agreement — {deal.get('property_address', 'lot')}",
        "template_uuid": template_id,
        "recipients": [{
            "email": seller_email, "first_name": first, "last_name": last,
            "role": "Seller",
        }],
        "tokens": [
            {"name": "property_address", "value": str(deal.get("property_address", ""))},
            {"name": "agreed_price", "value": str(deal.get("agreed_price", ""))},
            {"name": "seller_name", "value": str(deal.get("seller_name", ""))},
            {"name": "apn", "value": str(deal.get("apn", ""))},
        ],
        "metadata": {"deal_id": deal.get("deal_id")},
    }
    status, resp = http_request("POST", f"{_BASE}/documents", body, api_key)
    if not (200 <= status < 300):
        return {"ok": False, "document_id": None, "status_code": status, "detail": resp[:300]}

    doc_id = json.loads(resp or "{}").get("id")

    # Wait for the template to render into a draft before sending.
    for _ in range(draft_poll):
        s, r = http_request("GET", f"{_BASE}/documents/{doc_id}", None, api_key)
        if 200 <= s < 300 and json.loads(r or "{}").get("status") == "document.draft":
            break
        sleep(2)

    s2, r2 = http_request(
        "POST", f"{_BASE}/documents/{doc_id}/send",
        {"message": "Please review and sign your cash purchase agreement.", "silent": False},
        api_key,
    )
    return {"ok": 200 <= s2 < 300, "document_id": doc_id, "status_code": s2, "detail": r2[:300]}
