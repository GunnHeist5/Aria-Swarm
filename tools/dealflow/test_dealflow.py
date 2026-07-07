"""tools/dealflow/test_dealflow.py — approval state machine + endpoints (offline).

Run: python -m tools.dealflow.test_dealflow
"""

from __future__ import annotations

import importlib
import os
import tempfile

from . import notify


class StubNotify:
    def __init__(self):
        self.approvals = []
        self.texts = []

    def send_approval(self, deal, *, token, chat_id, **_):
        self.approvals.append(deal)
        return 200, "ok"

    def send_text(self, text, *, token, chat_id, **_):
        self.texts.append(text)
        return 200, "ok"


class StubContractor:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []

    def create_and_send(self, deal, *, api_key, template_id, **_):
        self.calls.append(deal)
        if self.ok:
            return {"ok": True, "document_id": "DOC1", "status_code": 201, "detail": ""}
        return {"ok": False, "document_id": None, "status_code": 400, "detail": "bad template"}


def _svc():
    """Fresh service module pointed at a temp store."""

    fd = tempfile.mkdtemp()
    os.environ["DEALFLOW_STORE"] = os.path.join(fd, "pending.json")
    import tools.dealflow.service as service
    return importlib.reload(service)


DEAL = {
    "seller_name": "Jane Doe", "property_address": "3321 Beulah St",
    "county": "Harris", "state": "TX", "agreed_price": 9000,
    "contact": "jane@gmail.com",
}


def test_deal_agreed_stores_and_pushes():
    svc = _svc()
    n = StubNotify()
    deal_id = svc.on_deal_agreed(DEAL, token="t", chat_id="c", notifier=n)
    assert deal_id.startswith("D")
    assert len(n.approvals) == 1 and n.approvals[0]["deal_id"] == deal_id
    assert svc._load()[deal_id]["status"] == "pending_approval"


def test_accept_sends_contract():
    svc = _svc()
    n, c = StubNotify(), StubContractor(ok=True)
    deal_id = svc.on_deal_agreed(DEAL, token="t", chat_id="c", notifier=n)
    res = svc.on_decision("accept", deal_id, token="t", chat_id="c",
                          api_key="k", template_id="tpl", notifier=n, contractor=c)
    assert res["status"] == "contract_sent" and res["document_id"] == "DOC1"
    assert len(c.calls) == 1
    assert svc._load()[deal_id]["status"] == "contract_sent"


def test_decline_sends_nothing():
    svc = _svc()
    n, c = StubNotify(), StubContractor()
    deal_id = svc.on_deal_agreed(DEAL, token="t", chat_id="c", notifier=n)
    res = svc.on_decision("decline", deal_id, token="t", chat_id="c",
                          api_key="k", template_id="tpl", notifier=n, contractor=c)
    assert res["status"] == "declined"
    assert c.calls == []
    assert svc._load()[deal_id]["status"] == "declined"


def test_decision_idempotent():
    svc = _svc()
    n, c = StubNotify(), StubContractor(ok=True)
    deal_id = svc.on_deal_agreed(DEAL, token="t", chat_id="c", notifier=n)
    svc.on_decision("accept", deal_id, token="t", chat_id="c", api_key="k",
                    template_id="tpl", notifier=n, contractor=c)
    again = svc.on_decision("accept", deal_id, token="t", chat_id="c", api_key="k",
                            template_id="tpl", notifier=n, contractor=c)
    assert again["reason"] == "already_handled"
    assert len(c.calls) == 1  # not re-sent


def test_unknown_deal():
    svc = _svc()
    res = svc.on_decision("accept", "Dxxxx", token="t", chat_id="c",
                          api_key="k", template_id="tpl",
                          notifier=StubNotify(), contractor=StubContractor())
    assert res["reason"] == "unknown_deal"


def test_send_failure_reported():
    svc = _svc()
    n, c = StubNotify(), StubContractor(ok=False)
    deal_id = svc.on_deal_agreed(DEAL, token="t", chat_id="c", notifier=n)
    res = svc.on_decision("accept", deal_id, token="t", chat_id="c", api_key="k",
                          template_id="tpl", notifier=n, contractor=c)
    assert res["status"] == "send_failed"
    assert any("Couldn't send" in t for t in n.texts)


def test_signed_marks_and_fires():
    svc = _svc()
    n, c = StubNotify(), StubContractor(ok=True)
    deal_id = svc.on_deal_agreed(DEAL, token="t", chat_id="c", notifier=n)
    svc.on_decision("accept", deal_id, token="t", chat_id="c", api_key="k",
                    template_id="tpl", notifier=n, contractor=c)
    fired = []
    deal = svc.on_signed("DOC1", token="t", chat_id="c", notifier=n,
                         fire_event=lambda d: fired.append(d["deal_id"]))
    assert deal and deal["status"] == "signed"
    assert fired == [deal_id]
    assert any("SIGNED" in t for t in n.texts)


def test_parse_callback():
    update = {"callback_query": {"id": "q1", "data": "accept:D123",
                                 "message": {"chat": {"id": 42}}}}
    action, deal_id, cq_id, chat_id = notify.parse_callback(update)
    assert (action, deal_id, cq_id, chat_id) == ("accept", "D123", "q1", 42)


def test_format_deal_has_fields():
    text = notify.format_deal({**DEAL, "deal_id": "D1"})
    assert "Jane Doe" in text and "Beulah" in text and "$9,000" in text


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")
