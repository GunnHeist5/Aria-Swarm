"""tools/dealflow/agent.py — the Telegram deal desk with hands.

The plain chat could only talk about one stapled-on context card. This gives it
tools, turning the bot into a live operator's assistant: look up any lead in
the export, price any lot, list the deals in the approval pipeline, mark a deal
verbally agreed at a specific price (which sends the Accept/Decline contract
prompt), and suppress an opt-out.

The safety line is structural: no tool sends email, and ``agree_deal`` only
*asks* — it produces the Accept/Decline prompt; a contract still goes out ONLY
when the operator taps Accept (CRITICAL_GATE unchanged). The most a rogue chat
turn can do is show the operator a prompt they decline.

The loop is LangChain-agnostic on purpose: messages are plain role dicts and
the LLM is duck-typed (``bind_tools``/``invoke``/``tool_calls``), so the whole
thing unit-tests offline with a scripted stub.
"""

from __future__ import annotations

import json
import os

from tools.dealdesk.lookup import FileLookup
from tools.dealdesk.pricing import compute_offer_range
from tools.integrations import suppression as _suppression

from . import draft as _draft

AGENT_SYSTEM = """You are the deal-desk agent for ARIA Capital, a vacant-land \
wholesaling operation, chatting with the operator on Telegram. Be direct and \
brief — plain text, no headers, no markdown. You have tools: use them instead \
of guessing. You can look up any lead's lot and price band, list pending deals, \
record a verbally-agreed deal at a specific price (this only sends the operator \
an Accept/Decline prompt — a contract goes out ONLY after they tap Accept), and \
suppress an opted-out address. You cannot send emails — the operator sends from \
Instantly; when they need email text, write it for them (anchored at the \
opening number, never stating or exceeding the ceiling). Numbers from tools \
beat numbers from memory."""

SPECS = [
    {
        "name": "lookup_property",
        "description": "Find a lead's lot by seller email or street address. "
                       "Returns the deal card, price band, and any escalation.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string",
                                     "description": "seller email or street address"}},
            "required": ["query"],
        },
    },
    {
        "name": "list_pending_deals",
        "description": "List every deal in the approval pipeline with its status "
                       "(proposed, pending_approval, contract_sent, signed, declined).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "agree_deal",
        "description": "The operator confirmed a verbal agreement at a specific "
                       "price. Stores the deal and sends the Accept/Decline "
                       "contract prompt at that price. The contract is only sent "
                       "after the operator taps Accept.",
        "input_schema": {
            "type": "object",
            "properties": {
                "lead_email": {"type": "string"},
                "agreed_price": {"type": "number"},
            },
            "required": ["lead_email", "agreed_price"],
        },
    },
    {
        "name": "suppress_lead",
        "description": "Opt-out: add an email to the do-not-contact ledger and "
                       "remove it from the Instantly campaign.",
        "input_schema": {
            "type": "object",
            "properties": {"email": {"type": "string"}},
            "required": ["email"],
        },
    },
]


class Toolbox:
    """Tool implementations with every dependency injectable for tests."""

    def __init__(self, *, token: str, chat_id: str, export_path: str | None = None,
                 lookup_cls=FileLookup, service=None, suppression_mod=_suppression,
                 remover=None):
        self.token, self.chat_id = token, chat_id
        self.export_path = export_path or os.environ.get("DEALDESK_EXPORT_PATH")
        self.lookup_cls = lookup_cls
        self._service = service
        self.suppression = suppression_mod
        self.remover = remover if remover is not None else _draft._remove_from_instantly

    @property
    def service(self):
        if self._service is None:
            from . import service as svc
            self._service = svc
        return self._service

    def specs(self) -> list[dict]:
        return SPECS

    def run(self, name: str, args: dict) -> dict:
        """Dispatch one tool call; errors become data, never exceptions."""

        fn = getattr(self, name, None)
        if name not in {s["name"] for s in SPECS} or fn is None:
            return {"error": f"unknown_tool:{name}"}
        try:
            return fn(**(args or {}))
        except TypeError as exc:
            return {"error": f"bad_args: {exc}"}
        except Exception as exc:  # noqa: BLE001
            return {"error": f"{type(exc).__name__}: {exc}"}

    # -- tools ---------------------------------------------------------------

    def _find(self, query: str):
        if not self.export_path:
            return None
        lookup = self.lookup_cls(self.export_path)
        query = (query or "").strip()
        if "@" in query:
            return lookup.find_by_email(query)
        return lookup.find(address=query)

    def lookup_property(self, query: str) -> dict:
        rec = self._find(query)
        if rec is None:
            return {"found": False, "note": "no match in the export by that email/address"}
        band = compute_offer_range(rec)
        return {
            "found": True,
            "card": _draft.deal_card(rec, band, None),
            "band": {"opening_offer": band.get("opening_offer"),
                     "max_offer": band.get("max_offer")},
            "escalate": band.get("escalate"),
            "escalate_reason": band.get("escalate_reason"),
        }

    def list_pending_deals(self) -> dict:
        store = self.service._load()
        deals = [
            {"deal_id": d.get("deal_id"), "address": d.get("property_address"),
             "price": d.get("agreed_price"), "status": d.get("status"),
             "contact": d.get("contact")}
            for d in store.values()
        ]
        return {"count": len(deals), "deals": deals}

    def agree_deal(self, lead_email: str, agreed_price: float) -> dict:
        rec = self._find(lead_email)
        if rec is None:
            return {"ok": False, "error": "lead not found in export — can't build "
                                          "the contract fields"}
        band = compute_offer_range(rec)
        ceiling = band.get("max_offer")
        if ceiling and float(agreed_price) > float(ceiling):
            return {"ok": False, "error": f"price {agreed_price} exceeds the ceiling "
                                          f"{ceiling} — not storing; escalate to the "
                                          "operator's own judgment explicitly"}
        deal = {
            "property_address": rec.address,
            "city": rec.city,
            "state": rec.state or "TX",
            "zip": rec.zip,
            "county": rec.county,
            "seller_name": _draft._owner_name(rec.raw),
            "contact": lead_email,
            "agreed_price": float(agreed_price),
        }
        deal_id = self.service.on_deal_agreed(deal, token=self.token,
                                              chat_id=self.chat_id)
        return {"ok": True, "deal_id": deal_id,
                "note": "Accept/Decline prompt sent — contract goes out only on Accept"}

    def suppress_lead(self, email: str) -> dict:
        self.suppression.add(email)
        try:
            removed = self.remover(email)
        except Exception:  # noqa: BLE001
            removed = None
        return {"suppressed": True, "removed_from_campaign": bool(removed)}


def _content(resp) -> str:
    content = getattr(resp, "content", resp)
    if isinstance(content, list):
        return "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        ).strip()
    return str(content).strip()


def run_agent(user_message: str, *, llm, toolbox: Toolbox,
              system: str = AGENT_SYSTEM, max_steps: int = 6) -> str:
    """Tool-calling loop over plain role-dict messages (LangChain-agnostic)."""

    runner = llm.bind_tools(toolbox.specs()) if hasattr(llm, "bind_tools") else llm
    msgs: list = [{"role": "system", "content": system},
                  {"role": "user", "content": user_message}]
    for _ in range(max_steps):
        resp = runner.invoke(msgs)
        msgs.append(resp)
        calls = list(getattr(resp, "tool_calls", None) or [])
        if not calls:
            return _content(resp)
        for call in calls:
            result = toolbox.run(call.get("name", ""), call.get("args") or {})
            msgs.append({"role": "tool", "tool_call_id": call.get("id", ""),
                         "content": json.dumps(result, default=str)[:4000]})
    return "(hit my tool-step limit — ask again more specifically)"
