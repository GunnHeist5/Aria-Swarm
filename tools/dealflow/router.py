"""tools/dealflow/router.py — HTTP endpoints for the deal-approval flow.

Mounted onto the existing receiver app (same service + Cloudflare tunnel), so:
    POST /deal-agreed        Trillet calls this when Jessica closes a verbal
    POST /telegram/callback  Telegram calls this when you tap Accept/Decline
    POST /pandadoc/webhook   PandaDoc calls this when the seller signs

Auth per surface, fail-closed:
  * /deal-agreed       — Bearer DEALFLOW_SECRET (Trillet sends it)
  * /telegram/callback — X-Telegram-Bot-Api-Secret-Token == TELEGRAM_WEBHOOK_SECRET
  * /pandadoc/webhook  — ?token=PANDADOC_WEBHOOK_SECRET
"""

from __future__ import annotations

import os
import threading

from fastapi import APIRouter, Header, HTTPException, Request

from tools.integrations.secrets import get_secret

from . import contracts, notify, service

router = APIRouter()


def _telegram():
    # Use a swarm-dedicated bot (SWARM_TELEGRAM_TOKEN) to avoid clashing with the
    # still-running Muffin gateway; fall back to MUFFIN_TELEGRAM_TOKEN.
    token = get_secret("SWARM_TELEGRAM_TOKEN") or get_secret("MUFFIN_TELEGRAM_TOKEN")
    return token, get_secret("JUSTIN_TELEGRAM_CHAT_ID")


def _pandadoc():
    # Reuse Muffin's proven key/template (env-first, vault fallback).
    return contracts.resolve_api_key(), contracts.resolve_template_id()


@router.post("/deal-agreed")
async def deal_agreed(request: Request, authorization: str = Header(default="")) -> dict:
    secret = os.environ.get("DEALFLOW_SECRET")
    if not secret or authorization != f"Bearer {secret}":
        raise HTTPException(status_code=401, detail="unauthorized")
    token, chat_id = _telegram()
    if not (token and chat_id):
        raise HTTPException(status_code=503, detail="telegram not configured")
    try:
        deal = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json")
    deal_id = service.on_deal_agreed(deal, token=token, chat_id=chat_id)
    return {"ok": True, "deal_id": deal_id}


@router.post("/telegram/callback")
async def telegram_callback(
    request: Request,
    x_telegram_bot_api_secret_token: str = Header(default=""),
) -> dict:
    secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET")
    if secret and x_telegram_bot_api_secret_token != secret:
        raise HTTPException(status_code=401, detail="unauthorized")
    try:
        update = await request.json()
    except Exception:
        return {"ok": True, "ignored": "no_json"}

    action, deal_id, cq_id, _ = notify.parse_callback(update)
    if not deal_id:
        # Not a button tap — maybe the operator talking to the deal desk.
        message = update.get("message") or {}
        token, chat_id = _telegram()
        if (message.get("text") and chat_id
                and str((message.get("chat") or {}).get("id")) == str(chat_id)):
            from . import chat as deal_chat

            # Answer off-thread: the LLM takes seconds, Telegram wants its 200 now.
            threading.Thread(
                target=deal_chat.handle_message, args=(message,),
                kwargs={"token": token, "chat_id": chat_id}, daemon=True,
            ).start()
            return {"ok": True, "chat": "answering"}
        return {"ok": True, "ignored": "no_callback"}
    token, chat_id = _telegram()
    if cq_id and token:
        notify.answer_callback(cq_id, "Working on it…", token=token)

    # A 'Deal agreed' tap on a reply-draft push promotes the proposed deal to
    # the Accept/Decline contract prompt; accept/decline dispatch the contract.
    if action == "agree":
        result = service.on_agree(deal_id, token=token, chat_id=chat_id)
        return {"ok": True, "result": result}

    api_key, template_id = _pandadoc()
    result = service.on_decision(
        action, deal_id, token=token, chat_id=chat_id,
        api_key=api_key, template_id=template_id)
    return {"ok": True, "result": result}


@router.post("/pandadoc/webhook")
async def pandadoc_webhook(request: Request, token: str | None = None) -> dict:
    secret = os.environ.get("PANDADOC_WEBHOOK_SECRET")
    if secret and token != secret:
        raise HTTPException(status_code=401, detail="unauthorized")
    tg_token, chat_id = _telegram()
    try:
        events = await request.json()
    except Exception:
        return {"ok": True, "ignored": "no_json"}

    handled = []
    for ev in (events if isinstance(events, list) else [events]):
        data = ev.get("data") or {}
        if data.get("status") == "document.completed":
            deal = service.on_signed(data.get("id"), token=tg_token, chat_id=chat_id)
            if deal:
                handled.append(deal["deal_id"])
    return {"ok": True, "signed": handled}
