"""tools/dealflow/test_dealflow.py — approval state machine + endpoints (offline).

Run: python -m tools.dealflow.test_dealflow
"""

from __future__ import annotations

import importlib
import json
import os
import tempfile

from . import notify


class StubNotify:
    def __init__(self):
        self.approvals = []
        self.assignment_approvals = []
        self.texts = []

    def send_approval(self, deal, *, token, chat_id, **_):
        self.approvals.append(deal)
        return 200, "ok"

    def send_assignment_approval(self, deal, *, token, chat_id, **_):
        self.assignment_approvals.append(deal)
        return 200, "ok"

    def send_text(self, text, *, token, chat_id, **_):
        self.texts.append(text)
        return 200, "ok"


class StubContractor:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []
        self.assignment_calls = []

    def create_and_send(self, deal, *, api_key, template_id, **_):
        self.calls.append(deal)
        if self.ok:
            return {"ok": True, "document_id": "DOC1", "status_code": 201, "detail": ""}
        return {"ok": False, "document_id": None, "status_code": 400, "detail": "bad template"}

    def create_and_send_assignment(self, deal, *, api_key, template_id, **_):
        self.assignment_calls.append((deal, template_id))
        if self.ok:
            return {"ok": True, "document_id": "ADOC1", "status_code": 201, "detail": ""}
        return {"ok": False, "document_id": None, "status_code": 400, "detail": "no template"}


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


def test_propose_then_agree_pushes_approval():
    svc = _svc()
    n = StubNotify()
    # Stage 1: the reply-draft push stashes a proposed deal (no notification).
    deal_id = svc.propose_deal(DEAL)
    assert svc._load()[deal_id]["status"] == "proposed"
    assert n.approvals == []
    # Stage 2: a 'Deal agreed' tap promotes it to the Accept/Decline prompt.
    res = svc.on_agree(deal_id, token="t", chat_id="c", notifier=n)
    assert res["status"] == "pending_approval"
    assert len(n.approvals) == 1 and n.approvals[0]["deal_id"] == deal_id
    # and Accept then dispatches the contract as usual
    c = StubContractor(ok=True)
    done = svc.on_decision("accept", deal_id, token="t", chat_id="c", api_key="k",
                           template_id="tpl", notifier=n, contractor=c)
    assert done["status"] == "contract_sent" and len(c.calls) == 1


def test_propose_deal_idempotent():
    svc = _svc()
    a = svc.propose_deal(DEAL)
    b = svc.propose_deal(DEAL)
    assert a == b and len(svc._load()) == 1


def test_agree_unknown_deal():
    svc = _svc()
    res = svc.on_agree("Dxxxx", token="t", chat_id="c", notifier=StubNotify())
    assert res["reason"] == "unknown_deal"


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


def test_buyer_confirmed_to_assignment_signed():
    svc = _svc()
    n, c = StubNotify(), StubContractor(ok=True)
    deal_id = svc.on_deal_agreed(DEAL, token="t", chat_id="c", notifier=n)
    # buyer locked in -> assignment Accept/Decline prompt (nothing sent yet)
    res = svc.on_buyer_confirmed(deal_id, "Cash Buyers LLC", "buyer@x.com", 15000,
                                 token="t", chat_id="c", notifier=n)
    assert res["ok"] and res["status"] == "pending_approval"
    assert len(n.assignment_approvals) == 1
    assert c.assignment_calls == []
    # Accept -> assignment sent to the buyer
    dec = svc.on_assignment_decision("accept_assign", deal_id, token="t",
                                     chat_id="c", api_key="k", template_id="atpl",
                                     notifier=n, contractor=c)
    assert dec["status"] == "contract_sent" and dec["document_id"] == "ADOC1"
    sent_deal, tpl = c.assignment_calls[0]
    assert tpl == "atpl" and sent_deal["buyer_email"] == "buyer@x.com"
    assert sent_deal["assignment_fee"] == 15000
    # idempotent
    again = svc.on_assignment_decision("accept_assign", deal_id, token="t",
                                       chat_id="c", api_key="k", template_id="atpl",
                                       notifier=n, contractor=c)
    assert again["reason"] == "already_handled" and len(c.assignment_calls) == 1
    # buyer signs -> assignment marked signed, wire-instructions ping
    deal = svc.on_signed("ADOC1", token="t", chat_id="c", notifier=n)
    assert deal["assignment_status"] == "signed"
    assert any("ASSIGNMENT SIGNED" in t for t in n.texts)


def test_assignment_decline_sends_nothing():
    svc = _svc()
    n, c = StubNotify(), StubContractor()
    deal_id = svc.on_deal_agreed(DEAL, token="t", chat_id="c", notifier=n)
    svc.on_buyer_confirmed(deal_id, "B LLC", "b@x.com", 9000,
                           token="t", chat_id="c", notifier=n)
    res = svc.on_assignment_decision("decline_assign", deal_id, token="t",
                                     chat_id="c", api_key="k", template_id="atpl",
                                     notifier=n, contractor=c)
    assert res["status"] == "declined" and c.assignment_calls == []


def test_contracts_assignment_payload():
    from . import contracts
    payloads = []

    def stub(method, url, payload, key):
        payloads.append((method, url, payload))
        if method == "POST" and url.endswith("/documents"):
            return 201, '{"id": "ADOC"}'
        if method == "GET":
            return 200, '{"status": "document.draft"}'
        return 200, "{}"

    deal = {**DEAL, "deal_id": "D1", "buyer_name": "Cash Buyers LLC",
            "buyer_email": "buyer@x.com", "assignment_fee": 15000}
    res = contracts.create_and_send_assignment(deal, api_key="k",
                                               template_id="atpl",
                                               http_request=stub,
                                               sleep=lambda s: None)
    assert res["ok"] and res["document_id"] == "ADOC"
    create = payloads[0][2]
    assert create["template_uuid"] == "atpl"
    assert create["recipients"][0]["email"] == "buyer@x.com"
    tokens = {t["name"]: t["value"] for t in create["tokens"]}
    assert tokens["assignment_fee"] == "15000"
    assert tokens["purchase_price"] == "9000"
    # missing template id fails closed with a clear message
    bad = contracts.create_and_send_assignment(deal, api_key="k", template_id="",
                                               http_request=stub)
    assert not bad["ok"] and "PANDADOC_ASSIGNMENT_TEMPLATE_ID" in bad["detail"]


def test_format_assignment_has_fields():
    text = notify.format_assignment({**DEAL, "deal_id": "D1",
                                     "buyer_name": "Cash Buyers LLC",
                                     "buyer_email": "buyer@x.com",
                                     "assignment_fee": 15000})
    assert "Cash Buyers LLC" in text and "$15,000" in text and "Beulah" in text


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


def test_contracts_build_fields_muffin_names():
    from . import contracts
    f = contracts.build_fields({**DEAL, "city": "Houston", "zip": "77000"})
    assert f["property_address"] == "3321 Beulah St"
    assert f["seller_email"] == "jane@gmail.com"
    assert f["purchase_price"] == "9000"
    assert f["buyer_name"] == "ARIA Capital LLC"
    assert "partner approval" in f["special_terms"]


def test_contracts_create_and_send_ok():
    from . import contracts
    calls = []

    def stub(method, url, payload, key):
        calls.append((method, url, payload))
        if method == "POST" and url.endswith("/documents"):
            return 201, '{"id": "DOC9"}'
        if method == "GET":
            return 200, '{"status": "document.draft"}'
        return 200, '{"status": "sent"}'

    res = contracts.create_and_send({**DEAL, "deal_id": "D1"}, api_key="k",
                                    template_id="tpl", http_request=stub,
                                    sleep=lambda s: None)
    assert res["ok"] and res["document_id"] == "DOC9"
    create = calls[0][2]
    # live-API shape: template_uuid + tokens + both template roles filled
    assert create["template_uuid"] == "tpl"
    assert {"name": "purchase_price", "value": "9000"} in create["tokens"]
    roles = [r["role"] for r in create["recipients"]]
    assert roles == ["Client", "Justin"]
    assert create["recipients"][0]["email"] == "jane@gmail.com"
    assert calls[1][0] == "GET"  # waited for document.draft
    assert calls[-1][1].endswith("/DOC9/send")


def test_contracts_send_waits_out_processing():
    from . import contracts
    states = iter(["document.uploaded", "document.uploaded", "document.draft"])
    calls = []

    def stub(method, url, payload, key):
        calls.append(method)
        if method == "POST" and url.endswith("/documents"):
            return 201, '{"id": "DOC9"}'
        if method == "GET":
            return 200, json.dumps({"status": next(states)})
        return 200, "{}"

    res = contracts.create_and_send({**DEAL, "deal_id": "D1"}, api_key="k",
                                    template_id="tpl", http_request=stub,
                                    sleep=lambda s: None)
    assert res["ok"]
    assert calls.count("GET") == 3  # polled until draft, then sent


def test_contracts_check_setup():
    from . import contracts
    os.environ["PANDADOC_API_KEY"] = "k-test"
    try:
        body = ('{"name": "Purchase Agreement", "roles": [{"name": "Client"}], '
                '"tokens": [{"name": "property_address"}, '
                '{"name": "purchase_price"}], '
                '"fields": [{"merge_field": "signature_1"}]}')
        rep = contracts.check_setup(http_request=lambda *a: (200, body))
        assert rep["ok"] and rep["api_key_source"] == "env"
        assert rep["tokens_matched"] == ["property_address", "purchase_price"]
        assert "seller_name" in rep["unmatched_ours"]
        assert rep["template_fields"] == ["signature_1"]
        bad = contracts.check_setup(http_request=lambda *a: (401, "denied"))
        assert not bad["ok"] and bad["status_code"] == 401
    finally:
        del os.environ["PANDADOC_API_KEY"]


def test_contracts_test_send_builds_test_deal():
    from . import contracts
    os.environ["PANDADOC_API_KEY"] = "k-test"
    try:
        payloads = []

        def stub(method, url, payload, key):
            payloads.append((url, payload))
            if method == "POST" and url.endswith("/documents"):
                return 201, '{"id": "DOCX"}'
            if method == "GET":
                return 200, '{"status": "document.draft"}'
            return 200, "{}"

        res = contracts.test_send("me@example.com", http_request=stub)
        assert res["ok"] and res["document_id"] == "DOCX"
        create = payloads[0][1]
        assert create["recipients"][0]["email"] == "me@example.com"
        tokens = {t["name"]: t["value"] for t in create["tokens"]}
        assert "SELF-TEST" in tokens["property_address"]
    finally:
        del os.environ["PANDADOC_API_KEY"]


def test_contracts_create_failure_reported():
    from . import contracts
    res = contracts.create_and_send(
        {**DEAL, "deal_id": "D1"}, api_key="k", template_id="tpl",
        http_request=lambda *a: (400, "bad template"))
    assert not res["ok"] and res["status_code"] == 400


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")
