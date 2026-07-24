"""tools/acquisition/reply/ingest.py — Instantly replies -> review queue.

Reuses the schema-verified puller (``tools.integrations.instantly_replies``)
and matches each reply to the ledger by email. Matching is deliberately
conservative: an email join is authoritative; anything else is a
``needs_manual`` queue item with NO apn — the agent never guesses which
parcel a stranger is talking about.

A matched owner may hold several parcels (same email, many APNs): the primary
match prefers the lead that has been screened, and every match rides along in
``all_matches`` so the drafter can see the whole holding.
"""

from __future__ import annotations

import json
import sqlite3

from ...integrations.instantly_replies import fetch_replies
from .. import ledger


def match_lead(lead_email: str,
               conn: sqlite3.Connection) -> tuple[sqlite3.Row | None, list[dict]]:
    """(primary_lead_row | None, all_matches). Email join only — never guess."""

    email = (lead_email or "").strip().lower()
    if not email:
        return None, []
    # email/email2 cover pre-migration rows; the emails JSON column carries
    # ALL export emails (sellers reply from any of them). The quoted LIKE
    # pattern matches the JSON-encoded string exactly, not substrings.
    rows = conn.execute(
        "SELECT * FROM leads WHERE lower(email)=? OR lower(email2)=? "
        "OR emails LIKE ? ORDER BY county_key, apn",
        (email, email, f'%"{email}"%')).fetchall()
    if not rows:
        return None, []
    primary = next((r for r in rows if r["verdict"]), rows[0])
    matches = [{"county_key": r["county_key"], "apn": r["apn"],
                "status": r["status"], "verdict": r["verdict"]} for r in rows]
    return primary, matches


def ingest_reply(reply: dict, *, conn: sqlite3.Connection) -> str:
    """Insert one normalized reply into the queue. Returns the item state.

    Idempotent on the Instantly email id — re-polling never duplicates.
    Matched leads move to ledger status ``replied`` (unless mid-deal or
    terminal — a seller replying twice mustn't regress ``offer_out``).
    """

    ledger.init_db(conn)
    primary, matches = match_lead(reply["lead_email"], conn)
    state = "new" if primary is not None else "needs_manual"
    reason = None if primary is not None else "no ledger match for reply email"
    now = ledger._stamp()

    cur = conn.execute(
        """INSERT OR IGNORE INTO review_queue
           (id, lead_email, county_key, apn, all_matches, eaccount, subject,
            ts, reply_text, state, reason, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (reply["id"], reply["lead_email"],
         primary["county_key"] if primary is not None else None,
         primary["apn"] if primary is not None else None,
         json.dumps(matches), reply.get("eaccount") or "",
         reply.get("subject") or "", reply.get("ts") or "",
         reply.get("reply_text") or "", state, reason, now, now))
    conn.commit()
    if not cur.rowcount:
        return "duplicate"

    if primary is not None:
        for m in matches:
            if m["status"] in ("negotiating", "offer_out", "contracted",
                               "closed", "dead", "suppressed", "replied"):
                continue
            ledger.set_status(m["county_key"], m["apn"], "replied",
                              note=f"reply:{reply['id']}", conn=conn)
    return state


def pull_and_ingest(*, api_key: str, campaign_id: str,
                    http_request=None, conn: sqlite3.Connection | None = None,
                    log=print) -> dict:
    """Fetch every campaign reply and queue the new ones."""

    kwargs = {"api_key": api_key, "campaign_id": campaign_id}
    if http_request is not None:
        kwargs["http_request"] = http_request
    replies = fetch_replies(**kwargs)

    own = conn is None
    conn = conn or ledger.connect()
    report = {"replies_seen": len(replies), "new": 0, "needs_manual": 0,
              "duplicate": 0}
    try:
        for reply in replies:
            state = ingest_reply(reply, conn=conn)
            if state == "duplicate":
                report["duplicate"] += 1
            elif state == "needs_manual":
                report["needs_manual"] += 1
                log(f"[replies] needs_manual (no ledger match): "
                    f"{reply['lead_email'][:1]}***")
            else:
                report["new"] += 1
    finally:
        if own:
            conn.close()
    return report
