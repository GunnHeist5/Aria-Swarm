"""tools/dealflow/test_agent.py — Telegram deal-desk agent loop (offline).

Run: python -m tools.dealflow.test_agent
"""

from __future__ import annotations

import json
import os
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["DEAL_CHAT_STORE"] = os.path.join(_tmp, "chat.json")
os.environ["SUPPRESSION_STORE"] = os.path.join(_tmp, "suppressed.json")

from tools.dealdesk.lookup import PropertyRecord

from . import agent as A


def _rec(email="jane@gmail.com"):
    return PropertyRecord(
        address="3321 Beulah St", city="Houston", state="TX", zip="77000",
        county="Harris", apn="051", est_value=334000, assessed_value=170000,
        open_loans_balance=None, lien_amount=None, mls_status=None,
        property_type="Vacant Land (General)",
        raw={"Email 1": email, "Owner 1 First Name": "Jane",
             "Owner 1 Last Name": "Doe"})


class StubLookup:
    def __init__(self, path):
        pass

    def find_by_email(self, email):
        return _rec(email) if "jane" in email else None

    def find(self, *, address=None, apn=None, owner_name=None):
        return _rec() if address and "beulah" in address.lower() else None


class Resp:
    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


class ScriptedLLM:
    """Returns canned responses in order; records messages + bound specs."""

    def __init__(self, script):
        self.script = list(script)
        self.turns = []
        self.bound = None

    def bind_tools(self, specs):
        self.bound = specs
        return self

    def invoke(self, msgs):
        self.turns.append(list(msgs))
        return self.script.pop(0)


class StubService:
    def __init__(self):
        self.agreed = []
        self.buyers = []
        self.store = {}

    def on_deal_agreed(self, deal, *, token, chat_id):
        self.agreed.append((deal, token, chat_id))
        return "D42"

    def on_buyer_confirmed(self, deal_id, buyer_name, buyer_email, fee, *,
                           token, chat_id):
        self.buyers.append((deal_id, buyer_name, buyer_email, fee))
        return {"ok": True, "status": "pending_approval", "deal_id": deal_id}

    def _load(self):
        return self.store


def _toolbox(**kw):
    kw.setdefault("token", "t")
    kw.setdefault("chat_id", "c")
    kw.setdefault("export_path", "ok")
    kw.setdefault("lookup_cls", StubLookup)
    kw.setdefault("service", StubService())
    kw.setdefault("remover", lambda e: True)
    return A.Toolbox(**kw)


def test_plain_answer_no_tools():
    llm = ScriptedLLM([Resp(content="Just an answer.")])
    out = A.run_agent("what's my band?", llm=llm, toolbox=_toolbox())
    assert out == "Just an answer."
    assert llm.bound == A.SPECS  # tools were offered


def test_lookup_tool_roundtrip():
    llm = ScriptedLLM([
        Resp(tool_calls=[{"name": "lookup_property",
                          "args": {"query": "jane@gmail.com"}, "id": "t1"}]),
        Resp(content="Beulah prices at open $91,000."),
    ])
    out = A.run_agent("price jane's lot", llm=llm, toolbox=_toolbox())
    assert out == "Beulah prices at open $91,000."
    # the tool result actually went back into the conversation
    tool_msg = llm.turns[1][-1]
    assert tool_msg["role"] == "tool" and tool_msg["tool_call_id"] == "t1"
    payload = json.loads(tool_msg["content"])
    assert payload["found"] and "Beulah" in payload["card"]
    assert payload["band"]["opening_offer"] == 91000


def test_agree_deal_sends_approval_at_that_price():
    svc = StubService()
    llm = ScriptedLLM([
        Resp(tool_calls=[{"name": "agree_deal",
                          "args": {"lead_email": "jane@gmail.com",
                                   "agreed_price": 95000}, "id": "t1"}]),
        Resp(content="Done — Accept/Decline sent at $95,000."),
    ])
    out = A.run_agent("we agreed at 95k with jane", llm=llm,
                      toolbox=_toolbox(service=svc))
    assert "95,000" in out
    deal, token, chat_id = svc.agreed[0]
    assert deal["agreed_price"] == 95000.0
    assert deal["property_address"] == "3321 Beulah St"
    assert deal["seller_name"] == "Jane Doe"
    assert (token, chat_id) == ("t", "c")


def test_agree_deal_refuses_above_ceiling():
    svc = StubService()
    box = _toolbox(service=svc)
    res = box.run("agree_deal", {"lead_email": "jane@gmail.com",
                                 "agreed_price": 500000})
    assert res["ok"] is False and "ceiling" in res["error"]
    assert svc.agreed == []  # nothing stored, no prompt sent


def test_suppress_tool():
    calls = []
    box = _toolbox(remover=lambda e: calls.append(e) or True)
    res = box.run("suppress_lead", {"email": "gone@x.com"})
    assert res == {"suppressed": True, "removed_from_campaign": True}
    assert calls == ["gone@x.com"]
    assert box.suppression.contains("gone@x.com")


def test_unknown_tool_and_bad_args_become_data():
    box = _toolbox()
    assert "unknown_tool" in box.run("rm_rf", {})["error"]
    assert "bad_args" in box.run("lookup_property", {"nope": 1})["error"]


def test_list_pending_deals():
    svc = StubService()
    svc.store = {"D1": {"deal_id": "D1", "property_address": "1811 Elysian St",
                        "agreed_price": 74300, "status": "pending_approval",
                        "contact": "h@x.com"}}
    res = _toolbox(service=svc).run("list_pending_deals", {})
    assert res["count"] == 1 and res["deals"][0]["status"] == "pending_approval"


def test_confirm_buyer_tool():
    svc = StubService()
    res = _toolbox(service=svc).run("confirm_buyer", {
        "deal_id": "D42", "buyer_name": "Cash Buyers LLC",
        "buyer_email": "buyer@x.com", "assignment_fee": 15000})
    assert res["ok"] and "Accept" in res["note"]
    assert svc.buyers == [("D42", "Cash Buyers LLC", "buyer@x.com", 15000)]


def test_step_limit():
    call = Resp(tool_calls=[{"name": "list_pending_deals", "args": {}, "id": "x"}])
    llm = ScriptedLLM([call] * 6)
    out = A.run_agent("loop forever", llm=llm, toolbox=_toolbox(), max_steps=6)
    assert "step limit" in out


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")
