"""tools/integrations/test_instantly_webhook.py — receiver behavior + security.

Run: python -m pytest tools/integrations/test_instantly_webhook.py
 or: python -m tools.integrations.test_instantly_webhook
"""

from __future__ import annotations

import importlib
import os

from starlette.testclient import TestClient


def _client(secret="whooksecret"):
    """Reload the app with a given secret and a stubbed notify (no swarm spawn)."""

    import tools.muffin_bridge as mb
    calls = []
    mb.notify_seller_reply = lambda lead, text, contact, *, detach=False, **extra: (
        calls.append({"lead": lead, "text": text, "detach": detach, "extra": extra}) or True
    )
    if secret is None:
        os.environ.pop("INSTANTLY_WEBHOOK_SECRET", None)
    else:
        os.environ["INSTANTLY_WEBHOOK_SECRET"] = secret
    import tools.integrations.instantly_webhook as wh
    importlib.reload(wh)
    return TestClient(wh.app), calls


def _post(c, payload, token="whooksecret", auth=None):
    headers = {"Authorization": f"Bearer {auth}"} if auth else {}
    url = "/instantly/reply" + (f"?token={token}" if token else "")
    return c.post(url, json=payload, headers=headers)


def test_valid_reply_fires_detached_with_campaign():
    c, calls = _client()
    r = _post(c, {"event_type": "reply_received", "lead_email": "jane@gmail.com",
                  "reply_text": "I'd take 15k", "campaign_id": "cid-1"})
    assert r.status_code == 200 and r.json()["queued"] == "jane@gmail.com"
    assert len(calls) == 1
    assert calls[0]["detach"] is True
    assert calls[0]["extra"]["campaign_id"] == "cid-1"


def test_bad_token_401_no_fire():
    c, calls = _client()
    r = _post(c, {"event_type": "reply_received", "lead_email": "x@y.com",
                  "reply_text": "hi"}, token="WRONG")
    assert r.status_code == 401
    assert calls == []


def test_header_auth_works():
    c, calls = _client()
    r = _post(c, {"event_type": "reply_received", "lead_email": "bob@yahoo.com",
                  "reply_text": "9k"}, token=None, auth="whooksecret")
    assert r.status_code == 200 and len(calls) == 1


def test_non_reply_event_ignored():
    c, calls = _client()
    r = _post(c, {"event_type": "email_opened", "lead_email": "a@b.com"})
    assert r.status_code == 200 and r.json()["ignored"] == "email_opened"
    assert calls == []


def test_automated_sender_filtered():
    c, calls = _client()
    r = _post(c, {"event_type": "reply_received",
                  "lead_email": "no-reply@google.com", "reply_text": "x"})
    assert r.status_code == 200 and r.json()["ignored"] == "automated_sender"
    assert calls == []


def test_missing_lead_email_ignored():
    c, calls = _client()
    r = _post(c, {"event_type": "reply_received", "reply_text": "hi"})
    assert r.status_code == 200 and r.json()["ignored"] == "no_lead_email"
    assert calls == []


def test_snippet_fallback_when_no_full_text():
    c, calls = _client()
    r = _post(c, {"event_type": "reply_received", "lead_email": "s@gmail.com",
                  "reply_text_snippet": "call me"})
    assert r.status_code == 200 and calls[0]["text"] == "call me"


def test_fail_closed_when_secret_unset():
    c, calls = _client(secret=None)
    r = _post(c, {"event_type": "reply_received", "lead_email": "a@b.com"},
              token="anything")
    assert r.status_code == 401
    assert calls == []


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")
