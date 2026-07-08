"""tools/dealflow/chat.py — talk to the deal desk from Telegram.

The pushes are one-way; this is the way back. A plain text message to the bot
("why did this escalate?", "counter her at 60k", "make it more direct") is
answered by the specialist model with the deal's full context loaded — the
property card, the seller's words, the price band, and the last few chat turns.

Which deal? Reply directly to a push (Telegram's reply-to carries the lead's
email in the quoted text), name an email in your message, or say nothing and
get the most recent one.

Hard line, enforced in the prompt AND by construction: chat explains, re-prices,
and re-drafts — it has no tool to send email or fire a contract. Sending stays
behind Instantly (you) and the Accept button (you).

Context ledger: ``~/.automaton/deal_chat.json`` — written by ``draft.py`` on
every push, read here. Only the operator's chat id is answered (router checks).
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

STORE = Path(os.environ.get(
    "DEAL_CHAT_STORE", os.path.expanduser("~/.automaton/deal_chat.json")))
MAX_HISTORY_TURNS = 8       # per lead, keeps the prompt bounded
MAX_ANSWER_CHARS = 3900     # under Telegram's 4096 message cap

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

CHAT_PROMPT = """You are the deal-desk assistant for a vacant-land wholesaling \
operator (ARIA Capital). The operator is texting you about ONE deal, whose full \
context is below. Answer in plain, direct language — short paragraphs, no \
headers, no fluff. You may: explain the numbers and escalation reasons, run \
quick math, re-draft the seller email in any register the operator asks for \
(anchored at the opening number, NEVER stating or exceeding the ceiling), and \
give negotiation advice. You may NOT send anything: you have no ability to send \
emails or contracts — if asked to send, say the operator sends from Instantly \
(emails) or taps the Accept button (contracts), and give them the text to use.

<deal_context>
{context}
</deal_context>

<recent_chat>
{history}
</recent_chat>

Operator's message: {question}

Reply (plain text, under 3500 characters):"""


def _load() -> dict:
    try:
        return json.loads(STORE.read_text())
    except Exception:
        return {"last_lead": None, "leads": {}}


def _save(data: dict) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    STORE.write_text(json.dumps(data, indent=2, default=str))


def remember(lead_email: str, **ctx) -> None:
    """Record/refresh a lead's deal context (called by draft.py on every push)."""

    lead_email = (lead_email or "").strip().lower()
    if not lead_email:
        return
    data = _load()
    entry = data["leads"].setdefault(lead_email, {})
    entry.update({k: v for k, v in ctx.items() if v is not None})
    entry["ts"] = time.time()
    data["last_lead"] = lead_email
    _save(data)


def resolve_lead(message: dict, data: dict | None = None) -> str | None:
    """Work out which lead the operator means.

    Priority: an email in the message they replied to (our pushes start with
    'New reply from <email>') → an email typed in their own message → the most
    recently pushed lead.
    """

    data = data if data is not None else _load()
    quoted = ((message.get("reply_to_message") or {}).get("text")) or ""
    m = _EMAIL_RE.search(quoted)
    if m and m.group(0).lower() in data["leads"]:
        return m.group(0).lower()
    m = _EMAIL_RE.search(message.get("text") or "")
    if m and m.group(0).lower() in data["leads"]:
        return m.group(0).lower()
    return data.get("last_lead")


def build_prompt(lead_email: str, question: str, data: dict | None = None) -> str:
    data = data if data is not None else _load()
    entry = data["leads"].get(lead_email, {})
    ctx_lines = [f"lead_email: {lead_email}"]
    for key in ("address", "read", "reply_text", "card", "draft", "band",
                "escalate_reason", "deal_id"):
        if entry.get(key):
            ctx_lines.append(f"{key}: {entry[key]}")
    history = entry.get("history") or []
    hist_text = "\n".join(f"{who}: {text}" for who, text in history) or "(none)"
    return CHAT_PROMPT.format(context="\n".join(ctx_lines), history=hist_text,
                              question=question)


def record_turn(lead_email: str, question: str, answer: str) -> None:
    data = _load()
    entry = data["leads"].setdefault(lead_email, {})
    history = entry.setdefault("history", [])
    history.extend([["operator", question], ["assistant", answer]])
    del history[:-2 * MAX_HISTORY_TURNS]
    _save(data)


def _default_llm():
    """Specialist model via the graph's routing layer (lazy — heavy import)."""

    from graph import SPECIALIST_MODEL, get_llm_backend

    return get_llm_backend(SPECIALIST_MODEL, None)


def _content(resp) -> str:
    content = getattr(resp, "content", resp)
    if isinstance(content, list):
        return "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        ).strip()
    return str(content).strip()


def handle_message(message: dict, *, token: str, chat_id: str,
                   llm=None, notifier=None) -> dict:
    """Answer one operator message. Synchronous; the router runs it off-thread."""

    if notifier is None:
        from . import notify as notifier  # noqa: PLC0415

    def say(text):
        notifier.send_text(text[:MAX_ANSWER_CHARS], token=token, chat_id=chat_id,
                           parse_mode=None)

    question = (message.get("text") or "").strip()
    if not question:
        return {"answered": False, "reason": "empty"}

    lead = resolve_lead(message)
    if not lead:
        say("I don't have any deal context yet — I learn a deal when its reply "
            "push goes out. Reply to a specific push (or include the seller's "
            "email) and ask again.")
        return {"answered": False, "reason": "no_lead"}

    try:
        llm = llm or _default_llm()
        answer = _content(llm.invoke(build_prompt(lead, question)))
    except Exception as exc:  # noqa: BLE001
        say(f"(deal chat hit an error: {type(exc).__name__} — try again)")
        return {"answered": False, "reason": "llm_error"}

    say(f"[{lead}]\n{answer}")
    record_turn(lead, question, answer)
    return {"answered": True, "lead": lead}
