"""tools/dealflow/draft.py — assisted reply drafting for inbound seller emails.

For each triaged reply: match the sender's email back to their lot, price it, and
draft a ready-to-send reply in Jessica Young's voice — anchored at the opening
offer, never above (or revealing) the hidden ceiling — then push it to the
operator's Telegram to review and send from Instantly. The human stays in
control (they send); the AI writes the argument and carries the right number.

All I/O (property lookup, LLM, Telegram) is injected so this is unit-tested
offline. Best-effort by design: a draft failure must never break reply triage.
"""

from __future__ import annotations

from tools.dealdesk.lookup import FileLookup
from tools.dealdesk.pricing import compute_offer_range

from . import notify as _notify

DRAFT_PROMPT = """You are Jessica Young, an acquisitions specialist at ARIA Capital, \
replying by email to a landowner who responded to our cash-offer outreach for their \
vacant lot. Write ONLY the email body — warm, concise, human, 3-5 sentences. No \
subject line, no signature block (added separately).

Rules:
- Anchor at the OPENING number. You may move up toward the CEILING if they push, but \
NEVER state, hint at, or exceed the ceiling, and never call any number a "maximum".
- If the seller named a price at or under the ceiling, you can warmly agree and move \
toward next steps (a simple cash purchase agreement, we cover closing).
- If their price is above the ceiling, make the value case (cash, as-is, no fees or \
commissions, we cover closing, land is illiquid, online estimates run high for bare \
lots) and offer near the opening / what you "can do" — do NOT meet their number.
- If no price yet, make a clean cash offer at the opening number.
- Never reveal assessed value, the formula, or that a number is a ceiling. Sound like \
a real person, not a template.

<seller_reply>{reply}</seller_reply>
<internal_numbers>opening={opening} ceiling={ceiling} — NEVER reveal or exceed the ceiling</internal_numbers>
<qualifier_read>{read}</qualifier_read>

Write the email body:"""


def _content(resp) -> str:
    content = getattr(resp, "content", resp)
    if isinstance(content, list):
        return "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        ).strip()
    return str(content).strip()


def draft_reply(reply_text: str, band: dict, read: str, *, llm) -> str:
    """Generate the reply email body from the price band + seller message."""

    prompt = DRAFT_PROMPT.format(
        reply=reply_text or "(no message text)",
        opening=band.get("opening_offer"),
        ceiling=band.get("max_offer"),
        read=read or "(no read)",
    )
    return _content(llm.invoke(prompt))


def _money(v) -> str:
    try:
        return f"${float(v):,.0f}"
    except (TypeError, ValueError):
        return str(v)


def draft_and_notify(lead_email: str, reply_text: str, read: str, *, export_path,
                     token, chat_id, llm, notifier=_notify, lookup_cls=FileLookup) -> dict:
    """Price the lot, draft a reply, and push it to Telegram. Never raises."""

    header = f"📩 New reply from {lead_email}\nRead: {read}\n"

    if not (token and chat_id):
        return {"drafted": False, "reason": "telegram_unconfigured"}

    # Plain text (no Markdown) so an LLM draft with * _ [ etc. can't break the send.
    def push(text):
        notifier.send_text(text, token=token, chat_id=chat_id, parse_mode=None)

    rec = None
    try:
        if export_path:
            rec = lookup_cls(export_path).find_by_email(lead_email)
    except Exception:
        rec = None

    if rec is None:
        push(header + "\n⚠️ Couldn't match this reply to a lot in the export — "
             "handle this one manually.")
        return {"drafted": False, "reason": "no_property"}

    band = compute_offer_range(rec)
    if band.get("escalate"):
        push(header + f"\n⚠️ Escalate ({band['escalate_reason']}) — don't auto-offer; "
             f"handle manually.\nProperty: {rec.address}")
        return {"drafted": False, "reason": band["escalate_reason"]}

    try:
        draft = draft_reply(reply_text, band, read, llm=llm)
    except Exception as exc:  # noqa: BLE001
        push(header + f"\n(couldn't auto-draft: {type(exc).__name__}) — but your band is "
             f"open {_money(band['opening_offer'])}, ceiling {_money(band['max_offer'])}.")
        return {"drafted": False, "reason": "draft_failed", "band": band}

    msg = (
        header
        + f"Property: {rec.address}, {rec.city or ''} {rec.state or ''}\n"
        + f"Your band: open {_money(band['opening_offer'])}, "
        + f"ceiling {_money(band['max_offer'])} (never quote the ceiling)\n\n"
        + "—— Draft reply (review & send from Instantly) ——\n"
        + draft
    )
    push(msg)
    return {"drafted": True, "band": band, "draft": draft}
