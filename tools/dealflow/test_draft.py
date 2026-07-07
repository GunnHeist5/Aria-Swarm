"""tools/dealflow/test_draft.py — assisted reply drafting (offline).

Run: python -m tools.dealflow.test_draft
"""

from __future__ import annotations

from tools.dealdesk.lookup import PropertyRecord

from . import draft as D


class StubNotify:
    def __init__(self):
        self.texts = []

    def send_text(self, text, *, token, chat_id, **_):
        self.texts.append(text)
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
        notifier=n, lookup_cls=StubLookup)
    assert res["drafted"] is True
    assert len(n.texts) == 1
    assert "Draft reply" in n.texts[0] and "Beulah" in n.texts[0]
    # the LLM prompt carried the band and never the raw ceiling into the seller text
    assert "opening=" in llm.prompts[0] and "ceiling=" in llm.prompts[0]


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


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")
