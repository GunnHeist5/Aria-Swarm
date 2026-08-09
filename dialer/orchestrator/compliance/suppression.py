"""The permanent internal do-not-contact ledger (DB-backed).

Suppression is by PHONE, not contact: if two contact rows share a number and
one opts out, neither may ever be dialed again. Inserts normalize to E.164 so
a webhook's "(614) 555-0100" and the dial gate's "+16145550100" always meet
in the same row. There is deliberately no delete function here — the schema
comment says the same — removing a suppression is a manual, human act.
"""

from __future__ import annotations

from .. import db
from ..config import Settings
from ..logging_utils import get_logger
from .phones import normalize_phone

log = get_logger(__name__)


def _mask(phone: str) -> str:
    """Last-4-only form for logs: suppression events are PII-adjacent."""
    return f"…{phone[-4:]}" if len(phone) >= 4 else "…"


def _canonical(phone: str) -> str:
    # Unparseable input is still suppressed verbatim: a malformed opt-out
    # must never be dropped on the floor because we couldn't normalize it.
    return normalize_phone(phone) or phone


def is_suppressed(cfg: Settings, phone_e164: str) -> bool:
    row = db.query_one(
        cfg,
        "SELECT 1 AS present FROM suppression WHERE phone_e164 = %s",
        (_canonical(phone_e164),),
    )
    return row is not None


def suppress(cfg: Settings, phone_e164: str, reason: str, source: str) -> bool:
    """Insert into the ledger; returns False when the number was already there.

    ON CONFLICT DO NOTHING keeps the FIRST suppression record (its reason and
    source are the audit trail for when contact became impermissible).
    """
    phone = _canonical(phone_e164)
    inserted = db.execute(
        cfg,
        """
        INSERT INTO suppression (phone_e164, reason, source)
        VALUES (%s, %s, %s)
        ON CONFLICT (phone_e164) DO NOTHING
        """,
        (phone, reason, source),
    )
    if inserted:
        log.info("suppressed %s (reason=%s source=%s)", _mask(phone), reason, source)
    return inserted > 0
