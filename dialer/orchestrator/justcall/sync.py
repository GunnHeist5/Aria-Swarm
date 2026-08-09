"""Campaign sync: JustCall Sales Dialer contacts → Postgres (SPEC §1).

Postgres is the planning source of truth; JustCall is only the feed. The sync
paginates the campaign per progress-status audience, upserts by JustCall
contact id, and keeps an incremental cursor (max created_at per audience) in
kv_state so routine runs never re-pull ~8k contacts. Rows are never dropped
for data problems: an unparseable phone keeps its raw text on the row and the
compliance gate blocks it with INVALID_PHONE, so the dry-run CSV surfaces the
problem instead of hiding it.

Company name lives in a JustCall custom field whose key must be discovered —
not guessed (SPEC §1). The discovered mapping is persisted to
kv_state['justcall.company_field'] and logged; ambiguity resolves to None and
the agent script falls back to "your business".
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator

from orchestrator import db
from orchestrator.config import Settings
from orchestrator.logging_utils import get_logger
from orchestrator.models import TouchType

from .client import JustCallClient

log = get_logger(__name__)

KV_COMPANY_FIELD = "justcall.company_field"
KV_CURSOR_PREFIX = "justcall.sync_cursor."

_PER_PAGE = 100

# Skipped contacts belong to no audience; stored as 'first' with
# progress_status='Skipped' so the planner can filter them out (contract).
_AUDIENCES: tuple[tuple[str, TouchType], ...] = (
    ("Undialed", TouchType.FIRST),
    ("Dialed", TouchType.SECOND),
    ("Skipped", TouchType.FIRST),
)

_UPSERT_SQL = """
INSERT INTO contacts (
    justcall_contact_id, campaign_id, name, phone_e164, email, company_name,
    contact_status, progress_status, touch_type, consent_basis, custom_fields,
    justcall_created_at, last_synced_at
) VALUES (
    %(justcall_contact_id)s, %(campaign_id)s, %(name)s, %(phone_e164)s,
    %(email)s, %(company_name)s, %(contact_status)s, %(progress_status)s,
    %(touch_type)s, %(consent_basis)s, %(custom_fields)s,
    %(justcall_created_at)s, now()
)
ON CONFLICT (justcall_contact_id) DO UPDATE SET
    campaign_id = EXCLUDED.campaign_id,
    name = EXCLUDED.name,
    phone_e164 = EXCLUDED.phone_e164,
    email = EXCLUDED.email,
    company_name = COALESCE(EXCLUDED.company_name, contacts.company_name),
    contact_status = EXCLUDED.contact_status,
    progress_status = EXCLUDED.progress_status,
    touch_type = EXCLUDED.touch_type,
    -- Never overwrite a non-NULL consent basis on re-sync (audit trail).
    consent_basis = COALESCE(contacts.consent_basis, EXCLUDED.consent_basis),
    custom_fields = EXCLUDED.custom_fields,
    justcall_created_at = EXCLUDED.justcall_created_at,
    last_synced_at = now()
RETURNING (xmax = 0) AS inserted
"""


@dataclass
class SyncStats:
    fetched: int
    created: int
    updated: int
    suppressed: int
    company_field_key: str | None


def discover_company_field(sample_contacts: list[dict], override: str | None) -> str | None:
    """Resolve which custom_fields key holds the company name.

    Never guesses blind: an explicit override wins; otherwise keys/labels are
    scored for company/business mentions and only an unambiguous winner is
    used. Ties or zero matches return None — better to say "your business" on
    a call than to read the wrong field on 8,000 of them.
    """
    if override:
        return override
    scores: dict[str, int] = {}
    for contact in sample_contacts:
        for field in contact.get("custom_fields") or []:
            if not isinstance(field, dict):
                continue
            key = str(field.get("key") or "").strip()
            if not key:
                continue
            text = f"{key} {field.get('label') or ''}".lower()
            score = (2 if "company" in text else 0) + (1 if "business" in text else 0)
            if score:
                scores[key] = max(scores.get(key, 0), score)
    if not scores:
        return None
    best = max(scores.values())
    winners = [key for key, score in scores.items() if score == best]
    return winners[0] if len(winners) == 1 else None


def extract_company_name(contact: dict, field_key: str | None) -> str | None:
    """Pull the company name out of a contact's custom_fields, or None."""
    if not field_key:
        return None
    for field in contact.get("custom_fields") or []:
        if isinstance(field, dict) and field.get("key") == field_key:
            value = field.get("value")
            if value is None:
                return None
            text = str(value).strip()
            return text or None
    return None


def run_sync(cfg: Settings, *, full: bool = False) -> SyncStats:
    """Sync every audience of the campaign into Postgres.

    Incremental by default: the client yields newest-first, so once a contact
    older than the stored cursor appears we abandon the iterator (no further
    pages are fetched). ``full=True`` ignores cursors and re-pulls everything.
    """
    client = JustCallClient(cfg)

    override = cfg.justcall_company_field_key or None
    stored = db.kv_get(cfg, KV_COMPANY_FIELD)
    stored_key = stored if isinstance(stored, str) and stored else None
    field_key = override or stored_key
    discovery_needed = field_key is None
    if override and stored != override:
        db.kv_set(cfg, KV_COMPANY_FIELD, override)
        log.info("JustCall company-name field pinned by config: custom_fields key %r", override)

    fetched = created = updated = suppressed = 0

    for progress_status, touch in _AUDIENCES:
        cursor_key = KV_CURSOR_PREFIX + progress_status
        cursor = None if full else _parse_ts(db.kv_get(cfg, cursor_key))
        max_seen = cursor

        contact_iter = client.fetch_campaign_contacts(
            progress_status=progress_status, per_page=_PER_PAGE
        )
        if discovery_needed:
            # Discovery needs a sample page up front (one HTTP page at most);
            # afterwards the stream stays lazy for cursor early-stops.
            buffered = list(itertools.islice(contact_iter, _PER_PAGE))
            if buffered:
                field_key = discover_company_field(buffered, None)
                discovery_needed = False
                db.kv_set(cfg, KV_COMPANY_FIELD, field_key)
                if field_key:
                    log.info(
                        "Discovered JustCall company-name field: custom_fields key %r "
                        "(persisted to kv_state[%r])",
                        field_key,
                        KV_COMPANY_FIELD,
                    )
                else:
                    log.warning(
                        "Could not unambiguously discover a company-name custom field; "
                        "company_name stays NULL and the script will say 'your business'. "
                        "Set JUSTCALL_COMPANY_FIELD_KEY to pin it."
                    )
            contact_stream: Iterator[dict] = itertools.chain(buffered, contact_iter)
        else:
            contact_stream = contact_iter

        for contact in contact_stream:
            created_at = _parse_ts(contact.get("created_at"))
            if cursor is not None and created_at is not None and created_at < cursor:
                break  # newest-first: everything from here on was synced before
            if contact.get("id") in (None, ""):
                log.warning("skipping JustCall contact without id (keys=%s)", sorted(contact))
                continue
            fetched += 1
            if _upsert_contact(
                cfg, contact, touch=touch, progress_status=progress_status, field_key=field_key
            ):
                created += 1
            else:
                updated += 1
            if str(contact.get("status") or "").strip().upper() == "DNCA":
                if _suppress_dnca(cfg, contact):
                    suppressed += 1
            if created_at is not None and (max_seen is None or created_at > max_seen):
                max_seen = created_at

        if max_seen is not None and max_seen != cursor:
            db.kv_set(cfg, cursor_key, max_seen.isoformat())

    log.info(
        "JustCall sync done: fetched=%d created=%d updated=%d suppressed=%d company_field=%r",
        fetched, created, updated, suppressed, field_key,
    )
    return SyncStats(
        fetched=fetched,
        created=created,
        updated=updated,
        suppressed=suppressed,
        company_field_key=field_key,
    )


# ----------------------------------------------------------------- internals


def _normalize_phone(raw: str) -> str | None:
    # Lazy: compliance is a sibling package (agent B) that may not exist yet
    # at import time; this module must stay importable/testable without it.
    from orchestrator.compliance.phones import normalize_phone

    return normalize_phone(raw)


def _suppress_dnca(cfg: Settings, contact: dict) -> bool:
    phone = _normalize_phone(str(contact.get("phone_number") or ""))
    if phone is None:
        # Nothing suppressable without a real number; the row still carries
        # contact_status=DNCA, which the gate blocks on its own (CONTACT_DNCA).
        return False
    from orchestrator.compliance.suppression import suppress  # lazy: sibling package

    return bool(suppress(cfg, phone, "dnca", "sync"))


def _upsert_contact(
    cfg: Settings,
    contact: dict,
    *,
    touch: TouchType,
    progress_status: str,
    field_key: str | None,
) -> bool:
    """Upsert one contact row; returns True when the row was newly inserted."""
    raw_phone = str(contact.get("phone_number") or "").strip()
    phone = _normalize_phone(raw_phone) if raw_phone else None
    consent = (
        cfg.consent_basis_first_touch
        if touch is TouchType.FIRST
        else cfg.consent_basis_second_touch
    ) or None  # NULL when unset — never invent a consent basis
    params = {
        "justcall_contact_id": int(contact["id"]),
        "campaign_id": cfg.justcall_campaign_id,
        "name": contact.get("name") or None,
        # Unparseable numbers keep the raw text: the row survives, the gate
        # blocks it with INVALID_PHONE, and the dry-run CSV shows the problem.
        "phone_e164": phone or raw_phone,
        "email": contact.get("email") or None,
        "company_name": extract_company_name(contact, field_key),
        "contact_status": contact.get("status") or None,
        "progress_status": progress_status,
        "touch_type": touch.value,
        "consent_basis": consent,
        "custom_fields": db.Jsonb(contact.get("custom_fields") or []),
        "justcall_created_at": _parse_ts(contact.get("created_at")),
    }
    rows = db.query(cfg, _UPSERT_SQL, params)
    return bool(rows and rows[0].get("inserted"))


def _parse_ts(value: object) -> datetime | None:
    """Parse JustCall timestamps ('YYYY-MM-DD HH:MM:SS' or ISO); assume UTC
    when the value is naive — JustCall does not attach an offset."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
