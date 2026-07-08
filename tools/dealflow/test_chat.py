"""tools/dealflow/test_chat.py — deal-desk Telegram chat (offline).

Run: python -m tools.dealflow.test_chat
"""

from __future__ import annotations

import importlib
import os
import tempfile


def _chat():
    """Fresh chat module pointed at a temp ledger."""

    os.environ["DEAL_CHAT_STORE"] = os.path.join(tempfile.mkdtemp(), "chat.json")
    import tools.dealflow.chat as chat
    return importlib.reload(chat)


class StubNotify:
    def __init__(self):
        self.texts = []

    def send_text(self, text, *, token, chat_id, **_):
        self.texts.append(text)
        return 200, "ok"


class StubLLM:
    def __init__(self, out="Because the liens exceed the threshold."):
        self.out = out
        self.prompts = []

    def invoke(self, prompt):
        self.prompts.append(prompt)
        out = self.out
        class M:  # noqa: D401
            content = out
        return M()


def _seed(chat):
    chat.remember("mona@gmail.com", address="0 Grand Estates Dr",
                  card="Assessed: $200,000\nLiens/loans: $150,000",
                  read="warm · they want 400k · action: offer",
                  escalate_reason="encumbered")
    chat.remember("jane@gmail.com", address="3321 Beulah St",
                  card="Assessed: $170,000", draft="Hi Jane...",
                  band={"opening_offer": 91000, "max_offer": 107100})


def test_resolve_from_reply_to_push():
    chat = _chat()
    _seed(chat)
    msg = {"text": "why did this escalate?",
           "reply_to_message": {"text": "📩 New reply from mona@gmail.com\nRead: ..."}}
    assert chat.resolve_lead(msg) == "mona@gmail.com"


def test_resolve_from_typed_email_and_fallback():
    chat = _chat()
    _seed(chat)
    assert chat.resolve_lead({"text": "what about mona@gmail.com again"}) == "mona@gmail.com"
    # no email anywhere -> most recent push wins
    assert chat.resolve_lead({"text": "counter at 95k"}) == "jane@gmail.com"


def test_prompt_carries_context_and_guardrail():
    chat = _chat()
    _seed(chat)
    p = chat.build_prompt("mona@gmail.com", "why encumbered?")
    assert "0 Grand Estates Dr" in p and "encumbered" in p
    assert "may NOT send anything" in p
    assert "why encumbered?" in p


def test_handle_message_answers_and_records():
    chat = _chat()
    _seed(chat)
    n, llm = StubNotify(), StubLLM()
    res = chat.handle_message(
        {"text": "why did mona@gmail.com escalate?"},
        token="t", chat_id="c", llm=llm, notifier=n)
    assert res == {"answered": True, "lead": "mona@gmail.com"}
    assert n.texts and n.texts[0].startswith("[mona@gmail.com]")
    assert "liens exceed" in n.texts[0]
    # turn recorded, and visible to the next prompt
    p2 = chat.build_prompt("mona@gmail.com", "and now?")
    assert "operator: why did mona@gmail.com escalate?" in p2


def test_handle_message_no_context_is_helpful():
    chat = _chat()
    n = StubNotify()
    res = chat.handle_message({"text": "hello?"}, token="t", chat_id="c",
                              llm=StubLLM(), notifier=n)
    assert res["answered"] is False and res["reason"] == "no_lead"
    assert "don't have any deal context" in n.texts[0]


def test_llm_error_reported_not_raised():
    chat = _chat()
    _seed(chat)
    class Boom:
        def invoke(self, p):
            raise TimeoutError("slow")
    n = StubNotify()
    res = chat.handle_message({"text": "hi"}, token="t", chat_id="c",
                              llm=Boom(), notifier=n)
    assert res["reason"] == "llm_error" and "TimeoutError" in n.texts[0]


def test_history_trims():
    chat = _chat()
    _seed(chat)
    for i in range(20):
        chat.record_turn("jane@gmail.com", f"q{i}", f"a{i}")
    hist = chat._load()["leads"]["jane@gmail.com"]["history"]
    assert len(hist) == 2 * chat.MAX_HISTORY_TURNS
    assert hist[-1] == ["assistant", "a19"]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")
