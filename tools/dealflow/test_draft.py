"""tools/dealflow/test_draft.py — assisted reply drafting (offline).

Run: python -m tools.dealflow.test_draft
"""

from __future__ import annotations

import os
import tempfile

# Point the deal-chat + suppression ledgers at temp files BEFORE importing
# draft (which imports chat, which resolves its store path at import time).
_tmp = tempfile.mkdtemp()
os.environ["DEAL_CHAT_STORE"] = os.path.join(_tmp, "chat.json")
os.environ["SUPPRESSION_STORE"] = os.path.join(_tmp, "suppressed.json")

from tools.dealdesk.lookup import PropertyRecord

from . import draft as D


class StubNotify:
    def __init__(self):
        self.texts = []
        self.buttons = None

    def send_text(self, text, *, token, chat_id, **_):
        self.texts.append(text)
        return 200, "ok"

    def send_with_buttons(self, text, buttons, *, token, chat_id, **_):
        self.texts.append(text)
        self.buttons = buttons
        return 200, "ok"


class StubLLM:
    def __init__(self, out="Hi Jane — thanks for getting back to me! I can do $49,000 cash, as-is, no fees."):
        self.out = out
        self.prompts = []

    def invoke(self, prompt):
        self.prompts.append(prompt)
        class M:  # noqa: D401
            content = self.out
        return M()


def _rec(email="jane@gmail.com", **kw):
    base = dict(address="3321 Beulah St", city="Houston", state="TX", zip="77000",
                county="Harris", apn="051-037-000-0011", est_value=334000,
                assessed_value=170000, open_loans_balance=None, lien_amount=None,
                mls_status=None, property_type="Vacant Land (General)",
                raw={"Email 1": email})
    base.update(kw)
    return PropertyRecord(**base)


class StubLookup:
    """Stands in for FileLookup(path).find_by_email(email)."""

    def __init__(self, path):
        self._rec = _rec() if path != "MISS" else None

    def find_by_email(self, email):
        return self._rec


def test_priced_lead_gets_draft_pushed():
    n, llm = StubNotify(), StubLLM()
    res = D.draft_and_notify(
        "jane@gmail.com", "I'd take 40k", "hot · they want 40k · action: offer",
        export_path="ok", token="t", chat_id="c", llm=llm,
        notifier=n, lookup_cls=StubLookup, proposer=lambda deal: "Dtest123")
    assert res["drafted"] is True
    assert len(n.texts) == 1
    assert "Draft reply" in n.texts[0] and "Beulah" in n.texts[0]
    # the LLM prompt carried the band and never the raw ceiling into the seller text
    assert "opening=" in llm.prompts[0] and "ceiling=" in llm.prompts[0]
    # a clean lot gets the 'Deal agreed -> send contract' button wired to the deal
    assert n.buttons and n.buttons[0][1] == "agree:Dtest123"
    assert res["deal_id"] == "Dtest123"


def test_button_send_failure_falls_back_to_plain():
    class RejectingNotify(StubNotify):
        def send_with_buttons(self, text, buttons, *, token, chat_id, **_):
            self.buttons = buttons
            return 400, "Bad Request: reply markup rejected"
    n = RejectingNotify()
    res = D.draft_and_notify(
        "jane@gmail.com", "ok", "warm · none · respond",
        export_path="ok", token="t", chat_id="c", llm=StubLLM(),
        notifier=n, lookup_cls=StubLookup, proposer=lambda deal: "D9")
    # the card still reached Telegram as plain text; the deal stayed stashed
    assert res["drafted"] is True and res["deal_id"] == "D9"
    assert len(n.texts) == 1 and "deal stashed as D9" in n.texts[0]


def test_proposer_receives_contract_fields():
    captured = {}
    n = StubNotify()
    D.draft_and_notify(
        "jane@gmail.com", "ok", "warm · none · respond",
        export_path="ok", token="t", chat_id="c", llm=StubLLM(),
        notifier=n, lookup_cls=StubLookup,
        proposer=lambda deal: captured.update(deal) or "D1")
    assert captured["contact"] == "jane@gmail.com"
    assert captured["property_address"] == "3321 Beulah St"
    assert captured["agreed_price"] == 91000  # anchored at the opening offer


def test_unmatched_email_notifies_no_draft():
    n = StubNotify()
    res = D.draft_and_notify(
        "ghost@nowhere.com", "hi", "warm · none · respond",
        export_path="MISS", token="t", chat_id="c", llm=StubLLM(),
        notifier=n, lookup_cls=StubLookup)
    assert res["drafted"] is False and res["reason"] == "no_property"
    assert "manually" in n.texts[0]


def test_escalate_band_no_autodraft():
    # A listed property -> compute_offer_range escalates -> no auto-offer.
    class ListedLookup(StubLookup):
        def __init__(self, path):
            self._rec = _rec(mls_status="ACTIVE")
    n = StubNotify()
    res = D.draft_and_notify(
        "jane@gmail.com", "still for sale", "hot · none · escalate",
        export_path="ok", token="t", chat_id="c", llm=StubLLM(),
        notifier=n, lookup_cls=ListedLookup)
    assert res["drafted"] is False
    assert "Escalate" in n.texts[0]
    # the escalate push still carries the full deal card (numbers, not just a code)
    assert "Assessed: $170,000" in n.texts[0]
    assert "No auto-band" in n.texts[0]
    assert "Draft reply" not in n.texts[0]  # but never a draft/button on escalations


def test_is_opt_out_detection():
    yes = ["STOP", "stop", "Please stop.", "STOP\n\nSent from my iPhone",
           "unsubscribe", "Remove me from your list", "take me off your list",
           "please stop emailing me", "do not contact me again", "opt out"]
    no = ["stop by the lot anytime, gate's open",
          "I want to stop paying taxes on this dirt — make me an offer",
          "what's your offer?", "", None]
    for t in yes:
        assert D.is_opt_out(t), f"should be opt-out: {t!r}"
    for t in no:
        assert not D.is_opt_out(t), f"should NOT be opt-out: {t!r}"


class StubSuppression:
    def __init__(self):
        self.entries = set()

    def add(self, email):
        self.entries.add(email.lower())

    def contains(self, email):
        return email.lower() in self.entries


def test_stop_reply_suppresses_no_card_no_draft_no_button():
    n, llm, sup = StubNotify(), StubLLM(), StubSuppression()
    removed = []
    res = D.draft_and_notify(
        "jane@gmail.com", "STOP", "invalid · none · escalate",
        export_path="ok", token="t", chat_id="c", llm=llm,
        notifier=n, lookup_cls=StubLookup, remember=lambda *a, **k: None,
        remover=lambda e: removed.append(e) or True, suppressor=sup)
    assert res["reason"] == "opt_out" and res["removed"] is True
    assert removed == ["jane@gmail.com"]
    assert sup.contains("jane@gmail.com")
    assert "OPT-OUT" in n.texts[0] and "Auto-removed" in n.texts[0]
    # no pricing, no drafting, no button — and the LLM was never called
    assert "Deal card" not in n.texts[0] and n.buttons is None
    assert llm.prompts == []
    # any later reply from the same address is refused
    res2 = D.draft_and_notify(
        "jane@gmail.com", "actually what would you pay?", "warm · none · respond",
        export_path="ok", token="t", chat_id="c", llm=llm,
        notifier=n, lookup_cls=StubLookup, remember=lambda *a, **k: None,
        remover=lambda e: True, suppressor=sup)
    assert res2["reason"] == "suppressed"
    assert "opted out earlier" in n.texts[1] and llm.prompts == []


def test_opt_out_removal_failure_still_suppresses():
    n, sup = StubNotify(), StubSuppression()
    def boom(email):
        raise RuntimeError("instantly down")
    res = D.draft_and_notify(
        "jane@gmail.com", "unsubscribe", "invalid · none · escalate",
        export_path="ok", token="t", chat_id="c", llm=StubLLM(),
        notifier=n, lookup_cls=StubLookup, remember=lambda *a, **k: None,
        remover=boom, suppressor=sup)
    assert res["reason"] == "opt_out" and res["removed"] is False
    assert sup.contains("jane@gmail.com")
    assert "Couldn't auto-remove" in n.texts[0]


def test_telegram_unconfigured_is_safe():
    res = D.draft_and_notify("a@b.com", "hi", "warm", export_path="ok",
                             token="", chat_id="", llm=StubLLM(),
                             notifier=StubNotify(), lookup_cls=StubLookup)
    assert res["reason"] == "telegram_unconfigured"


def test_draft_reply_builds_prompt():
    llm = StubLLM("drafted body")
    out = D.draft_reply("I want 60k", {"opening_offer": 49000, "max_offer": 57000},
                        "hot · 60k · offer", llm=llm)
    assert out == "drafted body"
    assert "I want 60k" in llm.prompts[0]
    assert "OVERRIDE" not in llm.prompts[0]  # normal register


def test_big_gap_ask_switches_register():
    # Beulah ceiling is 107,100 -> a 400k ask (>2x) flips to the candid register.
    llm = StubLLM()
    D.draft_and_notify(
        "jane@gmail.com", "firm 400k", "warm · they want 400k · action: offer",
        export_path="ok", token="t", chat_id="c", llm=llm,
        notifier=StubNotify(), lookup_cls=StubLookup, proposer=lambda d: "D1",
        remember=lambda *a, **k: None)
    assert "OVERRIDE" in llm.prompts[0]
    # a near-band ask stays warm
    llm2 = StubLLM()
    D.draft_and_notify(
        "jane@gmail.com", "I'd take 95k", "hot · they want 95k · action: offer",
        export_path="ok", token="t", chat_id="c", llm=llm2,
        notifier=StubNotify(), lookup_cls=StubLookup, proposer=lambda d: "D1",
        remember=lambda *a, **k: None)
    assert "OVERRIDE" not in llm2.prompts[0]


def test_pushes_feed_the_chat_ledger():
    seen = {}
    D.draft_and_notify(
        "jane@gmail.com", "I'd take 40k", "hot · they want 40k · action: offer",
        export_path="ok", token="t", chat_id="c", llm=StubLLM(),
        notifier=StubNotify(), lookup_cls=StubLookup, proposer=lambda d: "D7",
        remember=lambda lead, **ctx: seen.update({"lead": lead, **ctx}))
    assert seen["lead"] == "jane@gmail.com"
    assert seen["deal_id"] == "D7" and "Beulah" in seen["address"]
    assert seen["band"]["max_offer"] == 107100 and seen["draft"]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")
