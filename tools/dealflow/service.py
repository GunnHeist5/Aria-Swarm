"""tools/dealflow/service.py — deal approval + contract dispatch orchestration.

The state machine behind the Telegram HITL:
    verbal agreed  -> pending_approval   (Telegram push sent)
    Accept         -> contract_sent      (PandaDoc emailed to seller)
    Decline        -> declined
    seller signs   -> signed             (fires the swarm's contract_signed event)

Pending deals persist to a small JSON file so the async Accept/Decline (which
arrives seconds-to-minutes later) can find the deal. All I/O (Telegram, PandaDoc,
swarm event) is injected so the whole machine is unit-tested offline.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from . import contracts as _contracts
from . import notify as _notify

STORE = Path(os.environ.get(
    "DEALFLOW_STORE", os.path.expanduser("~/.automaton/pending_deals.json")))
_REPO = Path(__file__).resolve().parents[2]


def _load() -> dict:
    try:
        return json.loads(STORE.read_text())
    except Exception:
        return {}


def _save(store: dict) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    STORE.write_text(json.dumps(store, indent=2, default=str))


def _deal_id(deal: dict) -> str:
    basis = f"{deal.get('property_address','')}|{deal.get('agreed_price','')}|{deal.get('contact','')}"
    return "D" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:10]


def propose_deal(deal: dict) -> str:
    """Store a not-yet-agreed deal (from the reply-draft push) so a later
    'Deal agreed' tap can promote it to the Accept/Decline contract prompt.

    Idempotent per deal_id (same property+price+contact re-proposes the same
    record). Does NOT notify — this is the silent Stage-1 stash behind the button.
    """

    deal = dict(deal)
    deal.setdefault("deal_id", _deal_id(deal))
    deal.setdefault("status", "proposed")
    store = _load()
    store[deal["deal_id"]] = deal
    _save(store)
    return deal["deal_id"]


def on_agree(deal_id: str, *, token: str, chat_id: str, notifier=_notify) -> dict:
    """A 'Deal agreed' tap on a Stage-1 push -> send the Accept/Decline contract
    prompt for the stored proposed deal. Idempotent once past pending_approval."""

    store = _load()
    deal = store.get(deal_id)
    if not deal:
        return {"ok": False, "reason": "unknown_deal"}
    if deal.get("status") not in (None, "proposed", "pending_approval"):
        return {"ok": True, "reason": "already_handled", "status": deal.get("status")}
    deal["status"] = "pending_approval"
    _save(store)
    notifier.send_approval(deal, token=token, chat_id=chat_id)
    return {"ok": True, "status": "pending_approval", "deal_id": deal_id}


def on_deal_agreed(deal: dict, *, token: str, chat_id: str, notifier=_notify) -> str:
    """Store the pending deal and push the Telegram approval. Returns the deal_id."""

    deal = dict(deal)
    deal.setdefault("deal_id", _deal_id(deal))
    deal["status"] = "pending_approval"
    store = _load()
    store[deal["deal_id"]] = deal
    _save(store)
    notifier.send_approval(deal, token=token, chat_id=chat_id)
    return deal["deal_id"]


def on_decision(action: str, deal_id: str, *, token: str, chat_id: str,
                api_key: str, template_id: str,
                notifier=_notify, contractor=_contracts) -> dict:
    """Handle an Accept/Decline callback. Idempotent per deal."""

    store = _load()
    deal = store.get(deal_id)
    if not deal:
        return {"ok": False, "reason": "unknown_deal"}
    if deal.get("status") != "pending_approval":
        return {"ok": True, "reason": "already_handled", "status": deal.get("status")}

    if action == "decline":
        deal["status"] = "declined"
        _save(store)
        notifier.send_text(
            f"❌ Declined — no contract sent for {deal.get('property_address', '?')}.",
            token=token, chat_id=chat_id)
        return {"ok": True, "status": "declined"}

    if action == "accept":
        result = contractor.create_and_send(deal, api_key=api_key, template_id=template_id)
        if result["ok"]:
            deal["status"] = "contract_sent"
            deal["document_id"] = result["document_id"]
            _save(store)
            notifier.send_text(
                f"✅ Contract sent to {deal.get('contact', 'the seller')} for "
                f"{deal.get('property_address', '?')}. You'll get a ping when they sign.",
                token=token, chat_id=chat_id)
            return {"ok": True, "status": "contract_sent", "document_id": result["document_id"]}
        deal["status"] = "send_failed"
        _save(store)
        notifier.send_text(
            f"⚠️ Couldn't send the contract (PandaDoc {result['status_code']}). "
            f"{result['detail']}", token=token, chat_id=chat_id)
        return {"ok": False, "status": "send_failed", "detail": result["detail"]}

    return {"ok": False, "reason": "unknown_action"}


def on_signed(document_id: str, *, token: str, chat_id: str,
              notifier=_notify, fire_event=None) -> dict | None:
    """PandaDoc reports a completed doc -> mark signed, notify, fire contract_signed."""

    store = _load()
    for deal in store.values():
        if deal.get("document_id") == document_id:
            deal["status"] = "signed"
            _save(store)
            notifier.send_text(
                f"🎉 SIGNED — {deal.get('property_address', '?')} for "
                f"${deal.get('agreed_price', '?')}. Next: submit to CLOSED Title.",
                token=token, chat_id=chat_id)
            (fire_event or _fire_contract_signed)(deal)
            return deal
    return None


def _fire_contract_signed(deal: dict) -> None:
    """Best-effort: fire the swarm's contract_signed event (dispo clock + closer)."""

    payload = {
        "deal_id": deal.get("deal_id"),
        "state": deal.get("state", "TX"),
        "address": deal.get("property_address"),
        "agreed_price": deal.get("agreed_price"),
    }
    try:
        subprocess.Popen(
            [sys.executable, str(_REPO / "main.py"), "--event", "contract_signed",
             "--payload", json.dumps(payload, default=str)],
            cwd=str(_REPO), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
    except Exception:
        pass
