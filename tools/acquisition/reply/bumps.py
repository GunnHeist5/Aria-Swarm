"""tools/acquisition/reply/bumps.py — offer aging follow-ups.

Every lead sitting in ``offer_out`` gets a drafted bump into the review
queue when the offer's age crosses each configured interval (default 7 and
21 days) — drafted, never sent; the human approves like any other item.

Aging runs from the ledger's offer_out transition timestamp, which the
human can backdate (`acquire ledger set ... --offered-on`), so offers that
predate the ledger age correctly. One bump per (lead, interval), ever —
idempotent on a deterministic queue id.

Bumps are deterministic templates (no LLM): the only number they may carry
is ``offer_amount``, the human's own recorded offer. They thread under the
seller's most recent reply when one exists (reply_to/eaccount inherited);
otherwise the draft is approve-and-copy — sent by hand from Instantly.
"""

from __future__ import annotations

import json
import sqlite3
import time

from .. import ledger
from ..config import AcquisitionConfig, DEFAULT_CONFIG
from .draft import COMPLIANCE_FOOTER

_TEMPLATES = {
    # first nudge: light, assumes good faith
    0: ("Hi{name},\n\n"
        "Just circling back on the offer I sent over{amount} — no pressure, "
        "I know these decisions take some thought. It still stands as "
        "written, and I'm happy to answer anything about the process, the "
        "title company, or the timeline.\n\n"
        "If it's easier to talk: (215) 484-0893, call or text.\n\n"
        "Justin Young\nARIA Capital LLC"),
    # final nudge: honest close-out with a soft deadline
    1: ("Hi{name},\n\n"
        "Last note from me on this one — I don't want to clutter your "
        "inbox. My offer{amount} is still on the table this week; after "
        "that I'll assume the timing isn't right and close the file. If "
        "circumstances change down the road, you're always welcome to "
        "reach back out.\n\n"
        "Either way, thanks for considering it.\n\n"
        "Justin Young\nARIA Capital LLC\n(215) 484-0893"),
}


def _offer_out_since(row: sqlite3.Row) -> float | None:
    """Epoch seconds of the LAST transition into offer_out (None if unparseable)."""

    try:
        history = json.loads(row["status_history"] or "[]")
    except ValueError:
        return None
    stamps = [h.get("at") for h in history if h.get("to") == "offer_out"]
    if not stamps:
        return None
    try:
        return time.mktime(time.strptime(stamps[-1], "%Y-%m-%d %H:%M:%S"))
    except (ValueError, TypeError):
        return None


def _draft_for(row: sqlite3.Row, tier: int) -> str:
    name = f" {row['first_name']}" if row["first_name"] else ""
    amount = (f" (${row['offer_amount']:,.0f})"
              if row["offer_amount"] else "")
    tier = min(tier, max(_TEMPLATES))
    return _TEMPLATES[tier].format(name=name, amount=amount) + COMPLIANCE_FOOTER


def queue_bumps(*, config: AcquisitionConfig = DEFAULT_CONFIG,
                conn: sqlite3.Connection | None = None,
                now: float | None = None, log=print) -> dict:
    """Queue due bump drafts for every aging offer_out lead. Idempotent."""

    own = conn is None
    conn = conn or ledger.connect()
    now = now if now is not None else time.time()
    report = {"offers_out": 0, "bumps_queued": 0, "due": []}
    try:
        ledger.init_db(conn)
        rows = conn.execute(
            "SELECT * FROM leads WHERE status='offer_out'").fetchall()
        report["offers_out"] = len(rows)
        for row in rows:
            since = _offer_out_since(row)
            if since is None:
                continue
            age_days = (now - since) / 86400.0
            due = [i for i in sorted(config.bump_days) if age_days >= i]
            if not due:
                continue
            # only the LATEST due tier — a backfilled 22-day offer gets the
            # final-nudge draft, not a stale 7-day nudge alongside it
            for tier, interval in ((len(due) - 1, due[-1]),):
                bump_id = f"bump-{row['county_key']}-{row['apn']}-{interval}d"
                # thread under the seller's most recent real reply, if any
                last = conn.execute(
                    "SELECT id, eaccount, subject FROM review_queue "
                    "WHERE county_key=? AND apn=? AND eaccount != '' "
                    "AND id NOT LIKE 'bump-%' ORDER BY ts DESC LIMIT 1",
                    (row["county_key"], row["apn"])).fetchone()
                stamp = ledger._stamp()
                cur = conn.execute(
                    """INSERT OR IGNORE INTO review_queue
                       (id, lead_email, county_key, apn, all_matches,
                        eaccount, subject, ts, reply_text, classification,
                        draft, draft_kind, state, reply_to,
                        created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (bump_id, row["email"] or "", row["county_key"],
                     row["apn"], "[]",
                     last["eaccount"] if last else "",
                     last["subject"] if last else "",
                     stamp, f"(offer aging: {age_days:.0f} days out)",
                     "bump", _draft_for(row, tier), "bump",
                     "pending_review",
                     last["id"] if last else None, stamp, stamp))
                if cur.rowcount:
                    report["bumps_queued"] += 1
                    report["due"].append(f"{row['county_key']}/{row['apn']} "
                                         f"@{interval}d")
                    log(f"[bumps] queued {interval}d bump for "
                        f"{row['county_key']}/{row['apn']} "
                        f"({age_days:.0f} days out)")
        conn.commit()
        return report
    finally:
        if own:
            conn.close()
