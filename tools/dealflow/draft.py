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

import re

from tools.dealdesk.lookup import FileLookup, _to_float
from tools.dealdesk.pricing import compute_offer_range
from tools.integrations import suppression

from . import chat as _chat
from . import notify as _notify

# Opt-out is a legal state, not a triage judgment — detect it deterministically.
# Strong phrases match anywhere; a bare "stop" only as the whole first line
# (our outreach says 'reply "STOP"'), so "stop by the lot anytime" stays a lead.
_OPT_OUT_STRONG = re.compile(
    r"unsubscrib|remove me|take me off|opt[ -]?out"
    r"|do not (contact|email|message)|don'?t (contact|email|message)"
    r"|stop (email|contact|messag|send|reach)", re.I)
_OPT_OUT_BARE_STOP = re.compile(r"^\W*(please\s+)?stop\W*$", re.I)


def is_opt_out(text: str | None) -> bool:
    if not text:
        return False
    if _OPT_OUT_STRONG.search(text):
        return True
    first_line = text.strip().splitlines()[0].strip()
    return bool(_OPT_OUT_BARE_STOP.match(first_line))

DRAFT_PROMPT = """You are Jessica Young, an acquisitions specialist at ARIA Capital, \
replying by email to a landowner who responded to our cash-offer outreach for their \
vacant lot. Write ONLY the email body — warm, concise, human, 3-5 sentences. No \
subject line, no signature block (added separately).

Rules:
- Anchor at the OPENING number. You may move up toward the CEILING if they push, but \
NEVER state, hint at, or exceed the ceiling, and never call any number a "maximum".
- If the seller named a price at or under the ceiling, you can warmly agree and move \
toward next steps (a simple cash purchase agreement, we cover closing).
- If their price is above the ceiling, make the value case and offer near the opening \
/ what you "can do" — do NOT meet their number. Value angles to draw from: all cash \
and as-is; NO agent commission (they keep the 6-10% they'd lose on a listing, so your \
number nets them close to a higher listed price); we cover closing costs; land is \
illiquid and sits for months-to-years with lots of competing listings; bigger tracts \
trade at a lower price-per-acre than small infill lots (acreage discount); and the \
carrying cost of holding vacant land (taxes on dirt producing nothing) plus the \
certainty of a fast close with no financing or survey games.
- If no price yet, make a clean cash offer at the opening number.
- Never reveal assessed value, the formula, or that a number is a ceiling. Sound like \
a real person, not a template.

<seller_reply>{reply}</seller_reply>
<internal_numbers>opening={opening} ceiling={ceiling} — NEVER reveal or exceed the ceiling</internal_numbers>
<qualifier_read>{read}</qualifier_read>
{extra}
Write the email body:"""

# When the ask is >2x the ceiling, warm mirroring reads weak — switch register.
BIG_GAP_RULES = """
OVERRIDE — the seller's ask is more than double our ceiling. Drop the warm \
template and write 3-4 candid, matter-of-fact sentences instead: say plainly \
that their number is far outside the range this lot actually trades in, give \
the OPENING number as where we'd be, ground it in one concrete fact (the \
county's assessed value, or what comparable lots really sell for), and leave \
the door open if their timing ever changes. No apologies, no chasing, no \
exclamation points, no fake enthusiasm — a real number from a real buyer, \
take it or keep it in a drawer.
"""


def _content(resp) -> str:
    content = getattr(resp, "content", resp)
    if isinstance(content, list):
        return "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        ).strip()
    return str(content).strip()


def draft_reply(reply_text: str, band: dict, read: str, *, llm,
                big_gap: bool = False) -> str:
    """Generate the reply email body from the price band + seller message."""

    prompt = DRAFT_PROMPT.format(
        reply=reply_text or "(no message text)",
        opening=band.get("opening_offer"),
        ceiling=band.get("max_offer"),
        read=read or "(no read)",
        extra=BIG_GAP_RULES if big_gap else "",
    )
    return _content(llm.invoke(prompt))


def _money(v) -> str:
    try:
        return f"${float(v):,.0f}"
    except (TypeError, ValueError):
        return str(v)


def _owner_name(raw: dict) -> str:
    """Best-effort seller name from the export's owner columns (for the contract)."""

    raw = raw or {}
    first = str(raw.get("Owner 1 First Name") or raw.get("Owner First Name") or "").strip()
    last = str(raw.get("Owner 1 Last Name") or raw.get("Owner Last Name") or "").strip()
    return " ".join(p for p in (first, last) if p)


def _parse_price(text: str | None) -> float | None:
    """Pull a dollar figure out of the qualifier read (e.g. 'they want 75k')."""

    if not text:
        return None
    m = re.search(r"\$?\s*([\d,]+(?:\.\d+)?)\s*([kK])?", text)
    if not m:
        return None
    try:
        n = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    if m.group(2):
        n *= 1000
    return n if 500 <= n <= 5_000_000 else None


def deal_card(rec, band: dict, ask: float | None) -> str:
    """A Fable-style brief from data we already have in the export — no APIs."""

    raw = rec.raw or {}
    lot = _to_float(raw.get("Lot Size Sqft"))
    assessed = rec.assessed_value
    last_amt = _to_float(raw.get("Last Sale Amount"))
    last_date = str(raw.get("Last Sale Recording Date") or "")[:10]

    lines = [f"📊 Deal card — {rec.address}, {rec.city or ''} {rec.state or ''}"]
    if lot:
        lines.append(f"Lot: {lot / 43560:.2f} ac ({lot:,.0f} sqft)")
    if assessed:
        psf = f" (${assessed / lot:.2f}/sqft)" if lot else ""
        lines.append(f"Assessed: {_money(assessed)}{psf}")
    if rec.est_value:
        lines.append(f"Est. value (PropStream): {_money(rec.est_value)}"
                     " — often what the seller is looking at")
    if ask:
        psf = f" (${ask / lot:.2f}/sqft)" if lot else ""
        lines.append(f"Their ask: {_money(ask)}{psf}")
    if last_amt:
        when = f" on {last_date}" if last_date else ""
        lines.append(f"Last sale: {_money(last_amt)}{when}  ← likely his floor")
    elif last_date:
        lines.append(f"Last sale: {last_date} (amount not in export — pull the deed for his floor)")
    owed = (rec.open_loans_balance or 0.0) + (rec.lien_amount or 0.0)
    if owed:
        lines.append(f"Liens/loans: {_money(owed)}")
    if band.get("opening_offer"):
        lines.append(f"Your band: open {_money(band['opening_offer'])}, "
                     f"ceiling {_money(band['max_offer'])} (never quote the ceiling)")
    else:
        lines.append(f"No auto-band — escalated ({band.get('escalate_reason')}); "
                     "price it manually if worth pursuing")
    lines += [
        "",
        "Verify before you counter:",
        "❓ Sold comps (not listings) — 3-5 similar lots, last 12mo (PropStream)",
        "❓ Flood zone — FEMA map by APN (AE zone = cut the numbers)",
        "❓ Access + utilities at the lot (landlocked/no-utilities kills demand)",
        "❓ Taxes current",
    ]
    return "\n".join(lines)


def _remove_from_instantly(email: str) -> bool | None:
    """Best-effort: delete an opted-out lead from the live campaign."""

    from tools.integrations.instantly import remove_lead_by_email
    from tools.integrations.secrets import get_secret

    api_key = get_secret("INSTANTLY_API_KEY")
    campaign_id = get_secret("INSTANTLY_CAMPAIGN_ID")
    if not (api_key and campaign_id):
        return None
    return remove_lead_by_email(email, api_key=api_key, campaign_id=campaign_id)


def draft_and_notify(lead_email: str, reply_text: str, read: str, *, export_path,
                     token, chat_id, llm, notifier=_notify, lookup_cls=FileLookup,
                     proposer=None, remember=None, remover=_remove_from_instantly,
                     suppressor=suppression) -> dict:
    """Price the lot, draft a reply, and push it to Telegram. Never raises.

    On a clean, priceable lot the push carries a '✅ Send contract' button: when
    the seller says yes, tapping it promotes the deal to the Accept/Decline
    contract prompt (via ``service.propose_deal`` -> ``on_agree``). ``proposer``
    is injected for offline tests; live it defaults to ``service.propose_deal``.
    """

    header = f"📩 New reply from {lead_email}\nRead: {read}\n"

    if not (token and chat_id):
        return {"drafted": False, "reason": "telegram_unconfigured"}

    def recall(**ctx):
        # Feed the deal-chat ledger so the operator can talk back about this
        # lead. Best-effort: chat is a convenience, never a failure source.
        try:
            (remember or _chat.remember)(lead_email, reply_text=reply_text,
                                         read=read, **ctx)
        except Exception:  # noqa: BLE001
            pass

    # Plain text (no Markdown) so an LLM draft with * _ [ etc. can't break the send.
    def push(text):
        status, body = notifier.send_text(text, token=token, chat_id=chat_id,
                                          parse_mode=None)
        print(f"[draft] telegram send status={status}"
              + ("" if status == 200 else f" body={str(body)[:200]}"))
        return status, body

    # Opt-out is checked BEFORE anything else: no pricing, no card, no draft,
    # no button — suppress, remove from the campaign, never contact again.
    if is_opt_out(reply_text):
        try:
            suppressor.add(lead_email)
        except Exception:  # noqa: BLE001
            pass
        try:
            removed = remover(lead_email)
        except Exception:  # noqa: BLE001
            removed = None
        note = ("Auto-removed from the Instantly campaign." if removed else
                "Couldn't auto-remove — delete them from the campaign in Instantly.")
        push(header + "\n🛑 OPT-OUT — suppressed. No reply, no offer, no further "
             "contact, ever. " + note)
        recall(address="(opted out — do not contact)", opt_out=True)
        return {"drafted": False, "reason": "opt_out", "removed": bool(removed)}

    try:
        if suppressor.contains(lead_email):
            push(header + "\n🛑 This address opted out earlier — do not contact.")
            return {"drafted": False, "reason": "suppressed"}
    except Exception:  # noqa: BLE001
        pass

    rec = None
    try:
        if export_path:
            rec = lookup_cls(export_path).find_by_email(lead_email)
    except Exception:
        rec = None

    if rec is None:
        push(header + "\n⚠️ Couldn't match this reply to a lot in the export — "
             "handle this one manually.")
        recall(address="(no match in export)")
        return {"drafted": False, "reason": "no_property"}

    band = compute_offer_range(rec)
    if band.get("escalate"):
        # Still send the full card — the operator needs the numbers (assessed,
        # est. value, liens) to judge the escalation, not just a reason code.
        card = deal_card(rec, band, _parse_price(read))
        push(header + f"\n⚠️ Escalate ({band['escalate_reason']}) — no auto-offer; "
             "your call.\n\n" + card)
        recall(address=rec.address, card=card,
               escalate_reason=band["escalate_reason"])
        return {"drafted": False, "reason": band["escalate_reason"]}

    ask = _parse_price(read)
    big_gap = bool(ask and band.get("max_offer") and ask > 2 * band["max_offer"])
    try:
        draft = draft_reply(reply_text, band, read, llm=llm, big_gap=big_gap)
    except Exception as exc:  # noqa: BLE001
        push(header + f"\n(couldn't auto-draft: {type(exc).__name__}) — but your band is "
             f"open {_money(band['opening_offer'])}, ceiling {_money(band['max_offer'])}.")
        return {"drafted": False, "reason": "draft_failed", "band": band}

    card = deal_card(rec, band, ask)
    msg = (
        header + "\n" + card
        + "\n\n—— Draft reply (review & send from Instantly) ——\n"
        + draft
        + "\n\nNegotiate by email first. When a price is actually agreed: tap the"
          " button to contract at the opening, or message me here — e.g."
          " \"agreed at 85k\" — to contract at the real number."
    )

    # Stash the deal behind the button so a later 'agreed' tap can fire the
    # Accept/Decline contract prompt (anchored at the opening number).
    deal_id = None
    try:
        if proposer is None:
            from . import service as _service
            proposer = _service.propose_deal
        deal = {
            "property_address": rec.address,
            "city": rec.city,
            "state": rec.state or "TX",
            "zip": rec.zip,
            "county": rec.county,
            "seller_name": _owner_name(rec.raw),
            "contact": lead_email,
            "agreed_price": band["opening_offer"],
        }
        deal_id = proposer(deal)
    except Exception:  # noqa: BLE001 — a stash failure must not drop the draft
        deal_id = None

    if deal_id:
        buttons = [(f"✅ Deal agreed — send contract @ {_money(band['opening_offer'])}",
                    f"agree:{deal_id}")]
        status, body = notifier.send_with_buttons(
            msg, buttons, token=token, chat_id=chat_id, parse_mode=None)
        print(f"[draft] telegram button-send status={status}"
              + ("" if status == 200 else f" body={str(body)[:200]}"))
        if status != 200:
            # Never lose the deal card to a rejected keyboard payload — resend plain.
            push(msg + f"\n\n(button failed — deal stashed as {deal_id})")
    else:
        push(msg)
    recall(address=rec.address, card=card, draft=draft, deal_id=deal_id,
           band={"opening_offer": band["opening_offer"], "max_offer": band["max_offer"]})
    return {"drafted": True, "band": band, "draft": draft, "card": card,
            "deal_id": deal_id}
