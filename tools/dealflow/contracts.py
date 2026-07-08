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
import time
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


def resolve_assignment_template_id() -> str:
    """Assignment (buyer) template — no default; must be set explicitly."""

    return os.environ.get("PANDADOC_ASSIGNMENT_TEMPLATE_ID", "")


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
                    http_request=_request, sleep=time.sleep) -> dict:
    """Create the document from the template and send it for e-sign.

    Live-API verified specifics (the original Muffin port had these wrong):
      * the create payload key is ``template_uuid`` (``template_id`` -> 400);
      * values fill via ``tokens`` ({{placeholders}}), not a ``fields`` map;
      * recipient roles must match the template's actual roles — this template
        uses ``Client`` (the seller) and ``Justin`` (the buyer signer), both
        env-overridable;
      * creation is async — poll until ``document.draft`` before sending.

    Returns ``{ok, document_id, status_code, detail}``.
    """

    fields = build_fields(deal)
    seller_role = os.environ.get("PANDADOC_SELLER_ROLE", "Client")
    buyer_role = os.environ.get("PANDADOC_BUYER_ROLE", "Justin")
    buyer_email = os.environ.get("PANDADOC_BUYER_EMAIL", fields["buyer_email"])

    recipients = [{
        "email": fields["seller_email"],
        "first_name": (fields["seller_name"].split() or [""])[0],
        "last_name": " ".join(fields["seller_name"].split()[1:]),
        "role": seller_role,
    }]
    if buyer_email:  # the buyer-side signer (a template role, so it must be filled)
        recipients.append({"email": buyer_email, "first_name": "Justin",
                           "last_name": "Yi", "role": buyer_role})

    payload = {
        "name": f"Purchase Agreement — {fields['property_address'] or 'Property'}",
        "template_uuid": template_id,
        "recipients": recipients,
        "tokens": [{"name": k, "value": str(v)} for k, v in fields.items()],
        "metadata": {"deal_id": deal.get("deal_id")},
    }
    return _dispatch_document(
        payload, api_key=api_key, http_request=http_request, sleep=sleep,
        message="Please review and sign your cash purchase agreement.")


def build_assignment_fields(deal: dict) -> dict:
    """Assignment Agreement (buyer contract) tokens — Muffin's buyer protocol."""

    return {
        "property_address": deal.get("property_address") or deal.get("address", ""),
        "property_city": deal.get("city", ""),
        "property_state": deal.get("state", ""),
        "property_zip": deal.get("zip", ""),
        "buyer_name": str(deal.get("buyer_name", "")),
        "buyer_email": deal.get("buyer_email", ""),
        "assignment_fee": str(deal.get("assignment_fee", "")),
        "purchase_price": str(deal.get("agreed_price") or deal.get("offer_price", "")),
        "closing_date": deal.get("closing_date", ""),
        "seller_name": str(deal.get("seller_name", "")),
        "assignor_name": "ARIA Capital LLC",
        "assignor_email": "justin@ariacapital.tech",
    }


def create_and_send_assignment(deal: dict, *, api_key: str, template_id: str,
                               http_request=_request, sleep=time.sleep) -> dict:
    """Send the Assignment Agreement to the confirmed buyer for e-sign."""

    if not template_id:
        return {"ok": False, "document_id": None, "status_code": 0,
                "detail": "PANDADOC_ASSIGNMENT_TEMPLATE_ID not set"}
    fields = build_assignment_fields(deal)
    buyer_role = os.environ.get("PANDADOC_ASSIGNMENT_BUYER_ROLE", "Client")
    assignor_role = os.environ.get("PANDADOC_ASSIGNMENT_ASSIGNOR_ROLE", "Justin")
    assignor_email = os.environ.get("PANDADOC_BUYER_EMAIL", fields["assignor_email"])

    buyer_names = fields["buyer_name"].split() or [""]
    recipients = [{
        "email": fields["buyer_email"],
        "first_name": buyer_names[0],
        "last_name": " ".join(buyer_names[1:]),
        "role": buyer_role,
    }]
    if assignor_email:
        recipients.append({"email": assignor_email, "first_name": "Justin",
                           "last_name": "Yi", "role": assignor_role})

    payload = {
        "name": f"Assignment Agreement — {fields['property_address'] or 'Property'}",
        "template_uuid": template_id,
        "recipients": recipients,
        "tokens": [{"name": k, "value": str(v)} for k, v in fields.items()],
        "metadata": {"deal_id": deal.get("deal_id"), "kind": "assignment"},
    }
    return _dispatch_document(
        payload, api_key=api_key, http_request=http_request, sleep=sleep,
        message="Please review and sign the assignment agreement.")


def _dispatch_document(payload: dict, *, api_key: str, http_request, sleep,
                       message: str) -> dict:
    """Shared create -> poll-to-draft -> send path (live-API verified)."""

    status, resp = http_request("POST", f"{_BASE}/documents", payload, api_key)
    if status != 201:
        return {"ok": False, "document_id": None, "status_code": status, "detail": resp[:300]}

    doc_id = json.loads(resp or "{}").get("id")

    # Creation is async: sending while status is document.uploaded returns 409.
    for _ in range(15):
        s, b = http_request("GET", f"{_BASE}/documents/{doc_id}", None, api_key)
        if s == 200 and json.loads(b or "{}").get("status") == "document.draft":
            break
        sleep(2)

    s2, r2 = http_request(
        "POST", f"{_BASE}/documents/{doc_id}/send",
        {"message": message, "silent": False},
        api_key,
    )
    return {"ok": 200 <= s2 < 300, "document_id": doc_id, "status_code": s2, "detail": r2[:300]}


# ---------------------------------------------------------------------------
# Self-verification — prove the PandaDoc path before a seller ever hits it
# ---------------------------------------------------------------------------


def check_setup(*, http_request=_request, template_id: str | None = None,
                our_fields: dict | None = None) -> dict:
    """Validate the key + template against the live PandaDoc API (no doc made)."""

    key = resolve_api_key()
    source = ("env" if os.environ.get("PANDADOC_API_KEY")
              else "muffin_vault_FALLBACK (COMPROMISED — rotate!)" if key
              else "MISSING")
    template_id = template_id if template_id is not None else resolve_template_id()
    out = {"api_key_source": source, "template_id": template_id, "ok": False}
    if not key:
        out["detail"] = "set PANDADOC_API_KEY in .env"
        return out
    if not template_id:
        out["detail"] = "template id not set"
        return out

    status, body = http_request("GET", f"{_BASE}/templates/{template_id}/details",
                                None, key)
    out["status_code"] = status
    if status != 200:
        out["detail"] = body[:300]
        return out

    data = json.loads(body or "{}")
    out["ok"] = True
    out["template_name"] = data.get("name")
    out["roles"] = [r.get("name") for r in (data.get("roles") or [])]
    # Templates fill via tokens ({{placeholders}}); fields are form inputs.
    token_names = {str(t.get("name") or "") for t in (data.get("tokens") or [])}
    field_names = {(f.get("merge_field") or f.get("name") or "")
                   for f in (data.get("fields") or [])}
    ours = set(our_fields if our_fields is not None else build_fields({}))
    out["template_tokens"] = sorted(n for n in token_names if n)
    out["template_fields"] = sorted(n for n in field_names if n)
    out["tokens_matched"] = sorted(ours & token_names)
    out["unmatched_ours"] = sorted(ours - token_names - field_names)
    # ok above means key+template reachable; fill_ok means a sent document
    # would actually carry deal data. A template with zero matching tokens
    # sends contracts with every merge value silently blank.
    out["fill_ok"] = bool(out["tokens_matched"])
    if not out["fill_ok"]:
        out["warning"] = (
            "NO deal data would be filled in: the template has no matching "
            "{{tokens}}. Open the template in the PandaDoc editor and add "
            "{{token}} placeholders named exactly as in unmatched_ours."
        )
    return out


def list_templates(*, http_request=_request) -> dict:
    """List templates on the PandaDoc account — name + id, for .env setup."""

    key = resolve_api_key()
    if not key:
        return {"ok": False, "detail": "set PANDADOC_API_KEY in .env"}
    status, body = http_request("GET", f"{_BASE}/templates?count=100", None, key)
    if status != 200:
        return {"ok": False, "status_code": status, "detail": body[:300]}
    results = json.loads(body or "{}").get("results") or []
    return {"ok": True, "templates": [
        {"id": t.get("id"), "name": t.get("name"),
         "date_modified": t.get("date_modified")} for t in results]}


def test_send(recipient_email: str, *, http_request=_request) -> dict:
    """Send a real test agreement to YOURSELF — sign it to fire the full chain."""

    deal = {
        "deal_id": "TEST-SELFCHECK",
        "property_address": "0 Test Ln (SELF-TEST — not a real deal)",
        "city": "Houston", "state": "TX", "zip": "77000",
        "seller_name": "Test Seller",
        "contact": recipient_email,
        "agreed_price": 12345,
    }
    return create_and_send(deal, api_key=resolve_api_key(),
                           template_id=resolve_template_id(),
                           http_request=http_request)


def main(argv: list[str] | None = None) -> int:
    import argparse

    try:
        from dotenv import load_dotenv

        load_dotenv(override=True)
    except ImportError:
        pass

    parser = argparse.ArgumentParser(description="PandaDoc contract-path self-check.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true",
                       help="validate the API key + purchase template (creates nothing)")
    group.add_argument("--check-assignment", action="store_true",
                       help="validate the assignment (buyer) template")
    group.add_argument("--list-templates", action="store_true",
                       help="list template names + ids on the PandaDoc account")
    group.add_argument("--test-send", metavar="EMAIL",
                       help="send a real TEST agreement to this address (yourself)")
    args = parser.parse_args(argv)

    if args.list_templates:
        report = list_templates()
        print(json.dumps(report, indent=2))
        return 0 if report["ok"] else 1

    if args.check:
        report = check_setup()
        print(json.dumps(report, indent=2))
        return 0 if report["ok"] and report.get("fill_ok") else 1

    if args.check_assignment:
        report = check_setup(template_id=resolve_assignment_template_id(),
                             our_fields=build_assignment_fields({}))
        print(json.dumps(report, indent=2))
        return 0 if report["ok"] and report.get("fill_ok") else 1

    result = test_send(args.test_send)
    print(json.dumps(result, indent=2))
    if result["ok"]:
        print(f"\nSent — check {args.test_send}. Signing it fires the "
              "PandaDoc webhook -> contract_signed -> dispo clock (if the "
              "webhook is registered). Or void the doc in PandaDoc.")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
