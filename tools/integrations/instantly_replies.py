"""tools/integrations/instantly_replies.py — pull campaign replies from Instantly.

The webhook (``instantly_webhook.py``) is the real-time sensor; this is the
**active puller and backstop**: it lists every reply the campaign has ever
received via the Instantly v2 ``/emails`` API and fires each *new* one through
the same ``seller_reply`` pipeline (triage read → deal card → draft → Telegram
push). Replies that predate the webhook — or that the webhook missed — get
their pushes here.

Dedup is a local seen-ledger keyed by Instantly's email id
(``~/.automaton/instantly_replies_seen.json``), so re-polling is free: a reply
is fired exactly once no matter how many times the poller runs. The graph's
own lead+reply-hash guard is a second layer behind it.

CLI (run on the VPS, where INSTANTLY_API_KEY lives in .env):
  python -m tools.integrations.instantly_replies            # dry-run: list replies
  python -m tools.integrations.instantly_replies --push     # fire pushes for NEW replies
  python -m tools.integrations.instantly_replies --raw      # dump 1st raw item (schema debug)
  python -m tools.integrations.instantly_replies --push --include-seen  # re-fire everything

Run it on a systemd timer (see INTEGRATION.md) for a poll-every-5-minutes
backstop. A reply caught by both the webhook and the first poll may push twice
(their body text can differ slightly); that redundancy is the point — a missed
deal costs more than a duplicate ping.
"""

from __future__ import annotations

import argparse
import html as _html
import json
import os
import re
import urllib.parse
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(override=True)
except ImportError:
    pass

from tools.muffin_bridge import looks_automated, notify_seller_reply

from .instantly import USER_AGENT, _http_request, _mask_email  # noqa: F401 (UA doc)
from .secrets import SecretError, get_secret

EMAILS_URL = "https://api.instantly.ai/api/v2/emails"
SEEN_STORE = Path(os.environ.get(
    "INSTANTLY_SEEN_STORE", os.path.expanduser("~/.automaton/instantly_replies_seen.json")))
MAX_REPLY_CHARS = 4000  # keep the qualifier prompt sane on long threads

# Instantly ue_type: 1 = sent from campaign, 2 = received, 3 = sent manually.
_RECEIVED_UE_TYPE = 2


def _strip_html(text: str) -> str:
    text = re.sub(r"<(style|script)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return _html.unescape(re.sub(r"[ \t]+", " ", text)).strip()


def extract_reply(item: dict) -> dict | None:
    """Normalize one Instantly email object to ``{id, lead_email, reply_text, ts, subject}``.

    Defensive across schema spellings; returns None for anything that isn't a
    received human reply (our own sends, automated senders, empty addresses).
    """

    ue_type = item.get("ue_type")
    if ue_type is not None and ue_type != _RECEIVED_UE_TYPE:
        return None  # our own campaign/manual send, not a reply

    sender = (
        item.get("from_address_email")
        or item.get("from_email")
        or item.get("from")
        or ""
    )
    # "Jane Doe <jane@x.com>" -> jane@x.com
    m = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", str(sender))
    lead_email = m.group(0).lower() if m else ""
    if not lead_email or looks_automated(lead_email):
        return None

    body = item.get("body") or {}
    if isinstance(body, dict):
        text = body.get("text") or _strip_html(body.get("html") or "")
    else:
        text = str(body)
    text = (text or item.get("content_preview") or "").strip()

    return {
        "id": str(item.get("id") or ""),
        "lead_email": lead_email,
        "reply_text": text[:MAX_REPLY_CHARS],
        "ts": str(item.get("timestamp_email") or item.get("timestamp_created") or ""),
        "subject": str(item.get("subject") or ""),
    }


def fetch_replies(*, api_key: str, campaign_id: str, http_request=_http_request,
                  page_size: int = 100, max_pages: int = 50) -> list[dict]:
    """Every received reply in the campaign, oldest first. Fail-closed on auth."""

    raw_items: list[dict] = []
    starting_after = None
    for _ in range(max_pages):
        params = {"campaign_id": campaign_id, "email_type": "received",
                  "limit": page_size}
        if starting_after:
            params["starting_after"] = starting_after
        url = f"{EMAILS_URL}?{urllib.parse.urlencode(params)}"
        status, body = http_request("GET", url, None, api_key)
        if status in (401, 403):
            raise RuntimeError(f"instantly auth failed listing emails (HTTP {status})")
        if not 200 <= status < 300:
            raise RuntimeError(f"instantly /emails failed (HTTP {status}): {body[:200]}")
        data = json.loads(body or "{}")
        items = data.get("items") or []
        raw_items.extend(items)
        starting_after = data.get("next_starting_after")
        if not starting_after or not items:
            break

    replies = [r for r in (extract_reply(i) for i in raw_items) if r]
    replies.sort(key=lambda r: r["ts"])
    return replies


def _load_seen() -> set[str]:
    try:
        return set(json.loads(SEEN_STORE.read_text()))
    except Exception:
        return set()


def _save_seen(seen: set[str]) -> None:
    SEEN_STORE.parent.mkdir(parents=True, exist_ok=True)
    SEEN_STORE.write_text(json.dumps(sorted(seen)))


def sync(*, api_key: str, campaign_id: str, push: bool, include_seen: bool = False,
         http_request=_http_request, fire=notify_seller_reply,
         load_seen=_load_seen, save_seen=_save_seen) -> dict:
    """Fetch all replies, fire the pipeline for each new one, remember them."""

    replies = fetch_replies(api_key=api_key, campaign_id=campaign_id,
                            http_request=http_request)
    seen = load_seen()
    new = [r for r in replies if include_seen or r["id"] not in seen]

    fired = 0
    for r in new:
        line = (f"{r['ts'][:19] or '(no ts)':19}  {_mask_email(r['lead_email']):28} "
                f"{(r['reply_text'] or r['subject'])[:70]!r}")
        if not push:
            print(f"WOULD PUSH  {line}")
            continue
        # Synchronous (detach=False): one triage at a time, pushes arrive in
        # order, and this process's log shows each cycle's outcome.
        ok = fire(r["lead_email"], r["reply_text"] or "(empty reply)",
                  r["lead_email"], detach=False)
        print(f"{'PUSHED    ' if ok else 'FIRE FAIL '}  {line}")
        if ok:
            fired += 1
            seen.add(r["id"])
            save_seen(seen)  # per-reply, so a crash mid-run never re-fires done ones

    return {"total_replies": len(replies), "already_seen": len(replies) - len(new),
            "new": len(new), "fired": fired, "push": push}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pull Instantly campaign replies "
                                     "and push each through the seller_reply pipeline.")
    parser.add_argument("--push", action="store_true",
                        help="actually fire the pipeline (default: dry-run list)")
    parser.add_argument("--include-seen", action="store_true",
                        help="re-fire replies already in the seen-ledger")
    parser.add_argument("--raw", action="store_true",
                        help="print the first raw email object and exit (schema debug)")
    args = parser.parse_args(argv)

    try:
        api_key = get_secret("INSTANTLY_API_KEY", required=True)
        campaign_id = get_secret("INSTANTLY_CAMPAIGN_ID", required=True)
    except SecretError as exc:
        print(f"missing secret: {exc}")
        return 2

    if args.raw:
        url = f"{EMAILS_URL}?{urllib.parse.urlencode({'campaign_id': campaign_id, 'limit': 1})}"
        status, body = _http_request("GET", url, None, api_key)
        print(f"HTTP {status}\n{body[:3000]}")
        return 0

    report = sync(api_key=api_key, campaign_id=campaign_id, push=args.push,
                  include_seen=args.include_seen)
    print(f"\nreplies={report['total_replies']}  already_seen={report['already_seen']}  "
          f"new={report['new']}  fired={report['fired']}"
          + ("" if report["push"] else "   (dry-run — add --push to fire)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
