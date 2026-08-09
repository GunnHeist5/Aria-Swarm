"""Attempt counting for the per-business cap (SPEC §6: max total attempts
across BOTH touch types).

Counted by PHONE rather than contact id for the same reason suppression is:
the cap protects the business being called, and duplicate contact rows must
not double its allowance. Only 'canceled' rows are excluded — those never
reached (and can never reach) the phone. Everything else counts, including
'orphaned' and 'failed': when we cannot prove a dial did NOT happen, the
conservative reading is that it did.
"""

from __future__ import annotations

from .. import db
from ..config import Settings
from .phones import normalize_phone


def attempts_for_phone(cfg: Settings, phone_e164: str) -> int:
    phone = normalize_phone(phone_e164) or phone_e164
    row = db.query_one(
        cfg,
        """
        SELECT count(*) AS n FROM call_attempts
        WHERE to_phone_e164 = %s AND status <> 'canceled'
        """,
        (phone,),
    )
    return int(row["n"]) if row else 0
