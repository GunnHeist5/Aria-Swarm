"""tools/integrations/test_instantly_replies.py — reply puller (offline).

Run: python -m tools.integrations.test_instantly_replies
"""

from __future__ import annotations

import json

from . import instantly_replies as R


def _item(i="e1", frm="jane@gmail.com", text="I'd take 40k", ue=2, ts="2026-07-08T01:00:00"):
    return {"id": i, "from_address_email": frm, "ue_type": ue,
            "body": {"text": text}, "timestamp_email": ts, "subject": "Re: your lot"}


def _http(pages):
    """Stub http_request serving canned /emails pages."""

    calls = []

    def stub(method, url, payload, key):
        calls.append(url)
        page = pages[min(len(calls) - 1, len(pages) - 1)]
        return 200, json.dumps(page)

    stub.calls = calls
    return stub


def test_extract_received_reply():
    r = R.extract_reply(_item())
    assert r["lead_email"] == "jane@gmail.com" and r["reply_text"] == "I'd take 40k"


def test_extract_skips_our_sends_and_bots():
    assert R.extract_reply(_item(ue=1)) is None                       # our campaign send
    assert R.extract_reply(_item(frm="noreply@x.com")) is None        # automated
    assert R.extract_reply(_item(frm="")) is None                     # no address


def test_extract_html_fallback_and_name_addr():
    item = _item(frm="Jane Doe <JANE@Gmail.com>")
    item["body"] = {"html": "<p>Sure, <b>call me</b> &amp; we talk</p>"}
    r = R.extract_reply(item)
    assert r["lead_email"] == "jane@gmail.com"
    assert r["reply_text"] == "Sure, call me & we talk"


def test_quoted_thread_is_trimmed():
    # Shaped like the real reply seen live: seller's words, then the quoted
    # outreach (which contains our own pitch + the STOP line). Only the
    # seller's part may reach the qualifier.
    text = (
        "Hello\nAsking for a firm 400k\nThanks\nMona Dhall\n\n"
        "Sent from a handheld device. Please excuse any typing errors. \n\n"
        "> On Jul 7, 2026, at 4:14 PM, Justin Young <invest@x.org> wrote:\n"
        "> \n> Hi Monika,\n> I'm Jessica Young with ARIA Capital...\n"
        "> Not interested? Just reply \"STOP\" and I won't reach out again\n"
    )
    r = R.extract_reply(_item(text=text))
    assert "400k" in r["reply_text"]
    assert "STOP" not in r["reply_text"] and "Jessica" not in r["reply_text"]


def test_trim_variants():
    assert R._trim_quoted("Yes 50k works\nOn Mon, Jul 7, Jane <j@x.com> wrote:\n> hi") == "Yes 50k works"
    assert R._trim_quoted("call me\n-- Original Message --\nold stuff") == "call me"
    assert R._trim_quoted("no markers at all") == "no markers at all"
    # trimming everything falls back to content_preview in extract_reply
    item = _item(text="> fully quoted, nothing new")
    item["content_preview"] = "fallback preview"
    assert R.extract_reply(item)["reply_text"] == "fallback preview"


def test_fetch_paginates_and_sorts_oldest_first():
    stub = _http([
        {"items": [_item("e2", ts="2026-07-08T02:00:00")], "next_starting_after": "e2"},
        {"items": [_item("e1", ts="2026-07-08T01:00:00")]},
    ])
    replies = R.fetch_replies(api_key="k", campaign_id="c", http_request=stub)
    assert [r["id"] for r in replies] == ["e1", "e2"]
    assert len(stub.calls) == 2 and "campaign_id=c" in stub.calls[0]


def test_fetch_auth_fail_closed():
    try:
        R.fetch_replies(api_key="bad", campaign_id="c",
                        http_request=lambda *a: (401, "no"))
        assert False, "should raise"
    except RuntimeError as exc:
        assert "auth" in str(exc)


def _sync(pages, seen=None, push=True, fire_ok=True, include_seen=False):
    fired, store = [], set(seen or [])

    def fire(lead, text, contact, **kw):
        fired.append((lead, text))
        return fire_ok

    report = R.sync(api_key="k", campaign_id="c", push=push, include_seen=include_seen,
                    http_request=_http(pages), fire=fire,
                    load_seen=lambda: set(store), save_seen=lambda s: (store.clear(), store.update(s)))
    return report, fired, store


def test_sync_fires_each_new_reply_once():
    pages = [{"items": [_item("e1"), _item("e2", frm="bob@x.com", text="not interested")]}]
    report, fired, store = _sync(pages)
    assert report["fired"] == 2 and len(fired) == 2
    assert store == {"e1", "e2"}
    # second run: everything already seen -> nothing fired
    report2, fired2, _ = _sync(pages, seen=store)
    assert report2["new"] == 0 and fired2 == []


def test_sync_dry_run_fires_nothing():
    report, fired, store = _sync([{"items": [_item("e1")]}], push=False)
    assert report["new"] == 1 and fired == [] and store == set()


def test_sync_fire_failure_not_marked_seen():
    report, fired, store = _sync([{"items": [_item("e1")]}], fire_ok=False)
    assert report["fired"] == 0 and store == set()  # retried next poll


def test_sync_include_seen_refires():
    report, fired, _ = _sync([{"items": [_item("e1")]}], seen={"e1"}, include_seen=True)
    assert report["fired"] == 1 and len(fired) == 1


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")
