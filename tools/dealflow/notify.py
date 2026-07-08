"""tools/dealflow/notify.py — Telegram push with Accept/Decline for deal approval.

When Jessica closes a verbal, the operator gets a Telegram message with the deal
summary and two inline buttons. Tapping one sends a callback back to the swarm
(handled in ``router.py``). Contracts are CRITICAL_GATE — nothing goes out until
a human taps Accept.

All network is a single injectable ``http_post`` so the flow is fully testable
offline. Live creds come from ``MUFFIN_TELEGRAM_TOKEN`` / ``JUSTIN_TELEGRAM_CHAT_ID``.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

_API = "https://api.telegram.org/bot{token}/{method}"


def _post(url: str, payload: dict) -> tuple[int, str]:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def _money(v) -> str:
    try:
        return f"${float(v):,.0f}"
    except (TypeError, ValueError):
        return str(v)


def format_deal(deal: dict) -> str:
    return (
        "🏡 *New verbal agreement — approve to send the contract*\n\n"
        f"*Seller:* {deal.get('seller_name', '?')}\n"
        f"*Property:* {deal.get('property_address', '?')}\n"
        f"*County / State:* {deal.get('county', '?')}, {deal.get('state', '?')}\n"
        f"*Agreed price:* {_money(deal.get('agreed_price'))}\n"
        f"*Contact:* {deal.get('contact', '?')}\n\n"
        "Tap *Accept* to email the purchase agreement, or *Decline*."
    )


def format_assignment(deal: dict) -> str:
    return (
        "🤝 *Buyer locked in — approve to send the Assignment Agreement*\n\n"
        f"*Property:* {deal.get('property_address', '?')}\n"
        f"*Buyer:* {deal.get('buyer_name', '?')} ({deal.get('buyer_email', '?')})\n"
        f"*Assignment fee:* {_money(deal.get('assignment_fee'))}\n"
        f"*Seller price:* {_money(deal.get('agreed_price'))}\n\n"
        "Tap *Accept* to email the assignment agreement to the buyer, or *Decline*."
    )


def send_assignment_approval(deal: dict, *, token: str, chat_id: str,
                             http_post=_post) -> tuple[int, str]:
    payload = {
        "chat_id": chat_id,
        "text": format_assignment(deal),
        "parse_mode": "Markdown",
        "reply_markup": {"inline_keyboard": [[
            {"text": "✅ Accept & send assignment",
             "callback_data": f"accept_assign:{deal['deal_id']}"},
            {"text": "❌ Decline", "callback_data": f"decline_assign:{deal['deal_id']}"},
        ]]},
    }
    return http_post(_API.format(token=token, method="sendMessage"), payload)


def send_approval(deal: dict, *, token: str, chat_id: str, http_post=_post) -> tuple[int, str]:
    payload = {
        "chat_id": chat_id,
        "text": format_deal(deal),
        "parse_mode": "Markdown",
        "reply_markup": {"inline_keyboard": [[
            {"text": "✅ Accept & send contract", "callback_data": f"accept:{deal['deal_id']}"},
            {"text": "❌ Decline", "callback_data": f"decline:{deal['deal_id']}"},
        ]]},
    }
    return http_post(_API.format(token=token, method="sendMessage"), payload)


def send_text(text: str, *, token: str, chat_id: str,
              parse_mode: str | None = "Markdown", http_post=_post) -> tuple[int, str]:
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:  # None -> plain text, so arbitrary content can't break the send
        payload["parse_mode"] = parse_mode
    return http_post(_API.format(token=token, method="sendMessage"), payload)


def send_with_buttons(text: str, buttons, *, token: str, chat_id: str,
                      parse_mode: str | None = None, http_post=_post) -> tuple[int, str]:
    """Send ``text`` with one row of inline buttons.

    ``buttons`` is a list of ``(label, callback_data)`` tuples. Plain text by
    default (parse_mode=None) so an LLM-drafted body can't break the send.
    """

    payload = {
        "chat_id": chat_id,
        "text": text,
        "reply_markup": {"inline_keyboard": [
            [{"text": label, "callback_data": data} for label, data in buttons]
        ]},
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    return http_post(_API.format(token=token, method="sendMessage"), payload)


def answer_callback(callback_query_id: str, text: str, *, token: str, http_post=_post):
    return http_post(
        _API.format(token=token, method="answerCallbackQuery"),
        {"callback_query_id": callback_query_id, "text": text},
    )


def parse_callback(update: dict) -> tuple[str, str, str | None, int | None]:
    """Extract ``(action, deal_id, callback_query_id, chat_id)`` from an update."""

    cq = update.get("callback_query") or {}
    action, _, deal_id = (cq.get("data") or "").partition(":")
    chat_id = ((cq.get("message") or {}).get("chat") or {}).get("id")
    return action, deal_id, cq.get("id"), chat_id
