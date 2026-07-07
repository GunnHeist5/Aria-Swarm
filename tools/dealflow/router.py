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

from fastapi import APIRouter, Header, HTTPException, Request

from tools.integrations.secrets import get_secret

from . import notify, service

router = APIRouter()


def _telegram():
    return get_secret("MUFFIN_TELEGRAM_TOKEN"), get_secret("JUSTIN_TELEGRAM_CHAT_ID")


def _pandadoc():
    return get_secret("PANDADOC_API_KEY"), get_secret("PANDADOC_PURCHASE_TEMPLATE_ID")


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
        return {"ok": True, "ignored": "no_callback"}
    token, chat_id = _telegram()
    api_key, template_id = _pandadoc()
    if cq_id and token:
        notify.answer_callback(cq_id, "Working on it…", token=token)
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
