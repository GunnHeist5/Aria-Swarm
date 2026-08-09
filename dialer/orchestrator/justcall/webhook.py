"""Inbound JustCall webhook: signature verification + call-completed handling.

The second touch is event-driven (SPEC §2): a JustCall Workflow posts here
after a rep's call. Events duplicate and arrive out of order, so the handler
is idempotent by construction — a unique row in webhook_events gates every
side effect, and the attempt insert is ON CONFLICT DO NOTHING. Verification
fails closed: no configured secret means no event is ever trusted.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from orchestrator import db
from orchestrator.config import Settings
from orchestrator.logging_utils import get_logger

from . import writeback

log = get_logger(__name__)

# First present header wins (per the contract); JustCall's exact header name
# varies by workflow configuration.
_SIGNATURE_HEADERS = ("x-justcall-signature", "x-webhook-signature", "x-signature")

# Case-insensitive substring markers meaning "never call this person again".
_OPT_OUT_MARKERS = ("not interested - do not call", "do not call", "dnc", "remove")

_EVENT_INSERT_SQL = """
INSERT INTO webhook_events (source, external_id, event_type, payload)
VALUES ('justcall', %(external_id)s, %(event_type)s, %(payload)s)
ON CONFLICT (source, external_id, event_type) DO NOTHING
"""

_CONTACT_BY_JC_ID_SQL = """
SELECT id, justcall_contact_id, phone_e164, consent_basis
FROM contacts WHERE justcall_contact_id = %(justcall_contact_id)s
"""

_CONTACT_BY_PHONE_SQL = """
SELECT id, justcall_contact_id, phone_e164, consent_basis
FROM contacts WHERE phone_e164 = %(phone_e164)s ORDER BY id DESC LIMIT 1
"""

_MAX_ATTEMPT_SQL = """
SELECT COALESCE(MAX(attempt_number), 0) AS n
FROM call_attempts WHERE contact_id = %(contact_id)s AND touch_type = 'second'
"""

# ON CONFLICT DO NOTHING (no target) covers both the idempotency_key unique
# and the (contact_id, touch_type, attempt_number) unique — a racing duplicate
# webhook simply finds the follow-up already scheduled.
_ATTEMPT_INSERT_SQL = """
INSERT INTO call_attempts (
    idempotency_key, contact_id, touch_type, attempt_number, status,
    to_phone_e164, consent_basis, scheduled_for
) VALUES (
    %(idempotency_key)s, %(contact_id)s, 'second', %(attempt_number)s,
    'scheduled', %(to_phone_e164)s, %(consent_basis)s, %(scheduled_for)s
)
ON CONFLICT DO NOTHING
"""


def verify_signature(raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool:
    """Constant-time HMAC-SHA256 check of the raw body.

    Fails closed: no secret configured, or no signature header present, means
    the request is rejected. The digest header is accepted in hex or base64,
    with an optional "sha256=" prefix.
    """
    if not secret:
        return False
    lowered = {str(k).lower(): str(v) for k, v in headers.items()}
    provided: str | None = None
    for name in _SIGNATURE_HEADERS:
        if name in lowered:
            provided = lowered[name].strip()
            break
    if not provided:
        return False
    if provided.lower().startswith("sha256="):
        provided = provided[len("sha256="):].strip()
    digest = hmac.new(secret.encode(), raw_body, hashlib.sha256).digest()
    candidates = (digest.hex(), digest.hex().upper(), base64.b64encode(digest).decode())
    return any(hmac.compare_digest(provided, candidate) for candidate in candidates)


def handle_call_completed(cfg: Settings, payload: dict) -> str:
    """Process one (already signature-verified) call-completed event.

    Returns "duplicate" | "ignored" | "scheduled" | "suppressed".
    """
    external_id = _lookup(payload, ("call_id", "call_sid", "id"))
    if external_id is None:
        log.warning("justcall webhook without call id/call_sid — ignoring")
        return "ignored"
    external_id = str(external_id)
    event_type = str(_lookup(payload, ("event_type", "event", "type")) or "call_completed")

    inserted = db.execute(
        cfg,
        _EVENT_INSERT_SQL,
        {"external_id": external_id, "event_type": event_type, "payload": db.Jsonb(payload)},
    )
    if inserted == 0:
        return "duplicate"

    disposition = str(
        _lookup(payload, ("disposition", "call_disposition", "disposition_code")) or ""
    ).strip()
    lowered = disposition.lower()
    contact = _find_contact(cfg, payload)

    if any(marker in lowered for marker in _OPT_OUT_MARKERS):
        return _handle_opt_out(cfg, payload, contact, external_id)

    qualifying = {d.strip().lower() for d in cfg.justcall_qualifying_dispositions}
    if lowered and lowered in qualifying:
        if contact is None:
            log.info(
                "qualifying disposition %r on call %s but contact not synced yet — ignoring",
                disposition, external_id,
            )
            return "ignored"
        return _schedule_followup(cfg, contact)
    return "ignored"


# ----------------------------------------------------------------- internals


def _handle_opt_out(
    cfg: Settings, payload: dict, contact: dict | None, external_id: str
) -> str:
    """Permanent, immediate suppression + a DNCA writeback so reps see it too."""
    from orchestrator.compliance.suppression import suppress  # lazy: sibling package

    acted = False
    phone = contact["phone_e164"] if contact else _normalized_payload_phone(payload)
    if phone:
        suppress(cfg, phone, "opt_out", f"webhook:{external_id}")
        acted = True

    raw_jc_id: Any = (
        contact["justcall_contact_id"]
        if contact
        else _lookup(payload, ("justcall_contact_id", "contact_id"))
    )
    try:
        jc_id = int(raw_jc_id) if raw_jc_id is not None else None
    except (TypeError, ValueError):
        jc_id = None
    if jc_id is not None:
        writeback.enqueue_writeback(
            cfg, "justcall_dnca", None, {"justcall_contact_id": jc_id}
        )
        acted = True

    if not acted:
        log.warning(
            "opt-out disposition on call %s but no phone/contact to suppress", external_id
        )
        return "ignored"
    return "suppressed"


def _schedule_followup(cfg: Settings, contact: dict, *, now: datetime | None = None) -> str:
    """Create the second-touch attempt: delay + clamp into the legal window."""
    from orchestrator.compliance.windows import next_window_open  # lazy: sibling package

    now = now or datetime.now(timezone.utc)
    scheduled_for = now + timedelta(hours=cfg.followup_delay_hours)
    window_open = next_window_open(cfg, contact["phone_e164"], scheduled_for)
    if window_open is not None and window_open > scheduled_for:
        scheduled_for = window_open
    # Unknown zone (None) keeps the delayed time; the dial-time gate blocks
    # UNKNOWN_AREA_CODE anyway, so nothing illegal can slip through here.

    row = db.query_one(cfg, _MAX_ATTEMPT_SQL, {"contact_id": contact["id"]})
    attempt_number = int(row["n"] if row else 0) + 1
    inserted = db.execute(
        cfg,
        _ATTEMPT_INSERT_SQL,
        {
            "idempotency_key": f"contact:{contact['id']}:touch:second:attempt:{attempt_number}",
            "contact_id": contact["id"],
            "attempt_number": attempt_number,
            "to_phone_e164": contact["phone_e164"],
            "consent_basis": contact.get("consent_basis"),
            "scheduled_for": scheduled_for,
        },
    )
    if inserted == 0:
        log.info("follow-up already scheduled for contact %s (idempotent replay)", contact["id"])
    return "scheduled"


def _find_contact(cfg: Settings, payload: dict) -> dict | None:
    """Locate the synced contact row by JustCall id, else by normalized phone."""
    raw_jc_id = _lookup(payload, ("justcall_contact_id", "contact_id"))
    if raw_jc_id is not None:
        try:
            jc_id = int(raw_jc_id)
        except (TypeError, ValueError):
            jc_id = None
        if jc_id is not None:
            row = db.query_one(cfg, _CONTACT_BY_JC_ID_SQL, {"justcall_contact_id": jc_id})
            if row:
                return row
    phone = _normalized_payload_phone(payload)
    if phone:
        return db.query_one(cfg, _CONTACT_BY_PHONE_SQL, {"phone_e164": phone})
    return None


def _normalized_payload_phone(payload: dict) -> str | None:
    raw = _lookup(payload, ("contact_number", "phone_number", "phone", "to_number"))
    if not raw:
        return None
    from orchestrator.compliance.phones import normalize_phone  # lazy: sibling package

    return normalize_phone(str(raw))


def _lookup(payload: dict, keys: tuple[str, ...]) -> Any | None:
    """First non-empty value among keys, checked at the top level and inside
    the common JustCall nestings ('data', 'call')."""
    scopes: list[dict] = [payload]
    for nested in ("data", "call"):
        value = payload.get(nested)
        if isinstance(value, dict):
            scopes.append(value)
    for scope in scopes:
        for key in keys:
            value = scope.get(key)
            if value not in (None, ""):
                return value
    return None
