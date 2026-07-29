"""tools/acquisition/review.py — the human approval queue.

The ONLY path from a draft to a sent email runs through here, and every send
requires an explicit human action (``approve --send``). Approving without
``--send`` prints the exact payload and does nothing — the default is always
the safe half of the operation.

Sending uses Instantly's reply endpoint (``POST /api/v2/emails/reply``) so
the response threads under the seller's own email from the same mailbox that
received it. The approver-only decision footer is stripped before send;
the compliance footer stays.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time

from ..integrations.instantly import _http_request
from . import ledger
from .reply.draft import strip_approval_footer

REPLY_URL = "https://api.instantly.ai/api/v2/emails/reply"


def pending(conn: sqlite3.Connection | None = None,
            include_manual: bool = True) -> list[sqlite3.Row]:
    own = conn is None
    conn = conn or ledger.connect()
    try:
        ledger.init_db(conn)
        states = ("pending_review", "needs_manual") if include_manual \
            else ("pending_review",)
        marks = ",".join("?" for _ in states)
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        return conn.execute(
            f"SELECT * FROM review_queue WHERE state IN ({marks}) "
            "AND (snooze_until IS NULL OR snooze_until <= ?) ORDER BY ts",
            (*states, now)).fetchall()
    finally:
        if own:
            conn.close()


def _get(item_id: str, conn: sqlite3.Connection) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM review_queue WHERE id=?", (item_id,)).fetchone()
    if row is None:
        raise ledger.LedgerError(f"no review item {item_id!r}")
    return row


def approve(item_id: str, *, send: bool, api_key: str = "",
            edited_text: str | None = None,
            conn: sqlite3.Connection | None = None,
            http_request=_http_request, log=print) -> dict:
    """Approve a draft; with ``send`` actually deliver it via Instantly.

    Without ``send`` this prints the outbound payload for inspection and
    leaves the item pending. With ``send`` it posts the reply, marks the item
    ``sent``, and advances the lead (offer drafts -> ``offer_out``, holding/
    other -> ``negotiating``).
    """

    own = conn is None
    conn = conn or ledger.connect()
    try:
        item = _get(item_id, conn)
        if item["state"] not in ("pending_review", "needs_manual"):
            raise ledger.LedgerError(
                f"item {item_id} is {item['state']!r}, not approvable")
        body_text = strip_approval_footer(edited_text or item["draft"] or "")
        if not body_text.strip():
            raise ledger.LedgerError("empty draft — nothing to approve")
        eaccount = item["eaccount"]
        if not eaccount:
            # A lead entered by hand (a seller who replied from an address no
            # export carried) has no mailbox recorded, which blocked its offer
            # bumps entirely. Fall back to the mailbox this account actually
            # sends from: any other queue item's eaccount, else the configured
            # default. Threading still needs reply_to, so a bump with neither
            # is the only genuinely unsendable case.
            row = conn.execute(
                "SELECT eaccount FROM review_queue WHERE eaccount != '' "
                "AND eaccount IS NOT NULL ORDER BY created_at DESC "
                "LIMIT 1").fetchone()
            eaccount = (row["eaccount"] if row else "") or os.environ.get(
                "INSTANTLY_EACCOUNT", "")
            if eaccount:
                log(f"[review] no mailbox on this item — sending from "
                    f"{eaccount} (most recent known sender)")
        if not eaccount:
            raise ledger.LedgerError(
                "item has no eaccount and no fallback mailbox is known — set "
                "INSTANTLY_EACCOUNT in .env, or send from the Instantly UI")

        subject = item["subject"] or ""
        if subject and not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"
        payload = {
            # synthetic items (offer bumps) thread under the seller's last
            # real email via reply_to; reply items ARE that email (their id)
            "reply_to_uuid": item["reply_to"] or item["id"],
            "eaccount": eaccount,
            "subject": subject,
            "body": {"text": body_text},
        }

        if not send:
            log("[review] DRY-RUN — payload that WOULD be sent:")
            log(json.dumps(payload, indent=2)[:2000])
            return {"sent": False, "dry_run": True, "item": item_id}

        status, response = http_request("POST", REPLY_URL, payload, api_key)
        if not 200 <= status < 300:
            raise ledger.LedgerError(
                f"instantly reply send failed (HTTP {status}): {response[:300]}")

        conn.execute(
            "UPDATE review_queue SET state='sent', draft=?, updated_at=? "
            "WHERE id=?",
            ((edited_text or item["draft"]), ledger._stamp(), item_id))
        conn.commit()
        if item["county_key"] and item["apn"] and item["draft_kind"] != "bump":
            # bumps never move status — the lead stays offer_out
            new_status = "offer_out" if item["draft_kind"] == "offer" \
                else "negotiating"
            ledger.set_status(item["county_key"], item["apn"], new_status,
                              note=f"review:sent:{item_id}", conn=conn)
        log(f"[review] SENT {item_id} -> lead "
            f"{item['county_key']}/{item['apn']}")
        return {"sent": True, "dry_run": False, "item": item_id,
                "http_status": status}
    finally:
        if own:
            conn.close()


def reject(item_id: str, *, reason: str = "",
           conn: sqlite3.Connection | None = None) -> None:
    own = conn is None
    conn = conn or ledger.connect()
    try:
        _get(item_id, conn)
        conn.execute(
            "UPDATE review_queue SET state='rejected', reason=?, updated_at=? "
            "WHERE id=?", (reason or "rejected by human", ledger._stamp(), item_id))
        conn.commit()
    finally:
        if own:
            conn.close()


def snooze(item_id: str, *, days: float = 1.0,
           conn: sqlite3.Connection | None = None) -> str:
    until = time.strftime("%Y-%m-%d %H:%M:%S",
                          time.localtime(time.time() + days * 86400))
    own = conn is None
    conn = conn or ledger.connect()
    try:
        _get(item_id, conn)
        conn.execute(
            "UPDATE review_queue SET snooze_until=?, updated_at=? WHERE id=?",
            (until, ledger._stamp(), item_id))
        conn.commit()
    finally:
        if own:
            conn.close()
    return until


def sweep(*, note: str = "historical sweep",
          conn: sqlite3.Connection | None = None) -> int:
    """Bulk-close EVERY currently pending item (pending_review + needs_manual).

    For clearing pre-ledger history after the human has recorded statuses —
    an explicit operator action, never called by any automated path. Returns
    the number of items closed.
    """

    own = conn is None
    conn = conn or ledger.connect()
    try:
        ledger.init_db(conn)
        cur = conn.execute(
            "UPDATE review_queue SET state='rejected', reason=?, updated_at=? "
            "WHERE state IN ('pending_review', 'needs_manual')",
            (note, ledger._stamp()))
        conn.commit()
        return cur.rowcount
    finally:
        if own:
            conn.close()


def digest(conn: sqlite3.Connection | None = None) -> str:
    """Markdown digest of everything awaiting the human."""

    items = pending(conn)
    lines = [f"# Review queue — {len(items)} item(s) pending",
             ""]
    for it in items:
        head = (f"## `{it['id'][:12]}` · {it['lead_email']} · "
                f"{it['county_key'] or '?'}/{it['apn'] or '?'} · "
                f"{it['classification'] or it['state']}")
        lines += [head, "",
                  f"> {(it['reply_text'] or '')[:300]}", ""]
        if it["draft"]:
            lines += ["```", it["draft"][:1500], "```", ""]
        elif it["reason"]:
            lines += [f"_needs manual: {it['reason']}_", ""]
    return "\n".join(lines)
