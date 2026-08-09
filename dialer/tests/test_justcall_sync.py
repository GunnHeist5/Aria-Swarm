"""Sync logic: company-field discovery, upsert mapping, cursors, DNCA suppression.

No network and no Postgres: the JustCall client is replaced with an in-memory
fake, orchestrator.db functions are monkeypatched, and the (not-yet-written)
compliance sibling package is injected into sys.modules as fakes — sync
imports it lazily inside function bodies, so these stubs are all it ever sees.
"""

from __future__ import annotations

import json
import re
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import make_settings

from orchestrator import db
import orchestrator.justcall.sync as sync_mod
from orchestrator.justcall.sync import discover_company_field, extract_company_name, run_sync

FIXTURES = Path(__file__).parent / "fixtures"


# ------------------------------------------------------------------ helpers


def load_fixture_contacts() -> list[dict]:
    contacts: list[dict] = []
    for name in ("justcall_contacts_page0.json", "justcall_contacts_page1.json"):
        contacts.extend(json.loads((FIXTURES / name).read_text())["data"])
    return contacts


def fake_normalize(raw: str) -> str | None:
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return f"+1{digits}" if len(digits) == 10 else None


def install_fake_compliance(monkeypatch, *, suppress=None) -> None:
    pkg = types.ModuleType("orchestrator.compliance")
    phones = types.ModuleType("orchestrator.compliance.phones")
    phones.normalize_phone = fake_normalize
    suppression = types.ModuleType("orchestrator.compliance.suppression")
    suppression.suppress = suppress or (lambda cfg, phone, reason, source: True)
    pkg.phones, pkg.suppression = phones, suppression
    for name, module in {
        "orchestrator.compliance": pkg,
        "orchestrator.compliance.phones": phones,
        "orchestrator.compliance.suppression": suppression,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)


class FakeDb:
    def __init__(self) -> None:
        self.upserts: list[tuple[str, dict]] = []
        self.kv: dict[str, object] = {}
        self.insert_flags: list[bool] | None = None  # None → every upsert inserts

    def query(self, cfg, sql, params=None):
        assert "INSERT INTO contacts" in sql
        self.upserts.append((sql, params))
        flag = True if self.insert_flags is None else self.insert_flags[len(self.upserts) - 1]
        return [{"inserted": flag}]

    def kv_get(self, cfg, key):
        return self.kv.get(key)

    def kv_set(self, cfg, key, value):
        self.kv[key] = value


@pytest.fixture
def fake_db(monkeypatch) -> FakeDb:
    fake = FakeDb()
    monkeypatch.setattr(db, "query", fake.query)
    monkeypatch.setattr(db, "kv_get", fake.kv_get)
    monkeypatch.setattr(db, "kv_set", fake.kv_set)
    return fake


def fake_client_with(contacts_by_status: dict[str, list[dict]]):
    def fetch_campaign_contacts(*, progress_status=None, contact_status=None, per_page=100):
        yield from contacts_by_status.get(progress_status, [])

    return SimpleNamespace(fetch_campaign_contacts=fetch_campaign_contacts)


def use_client(monkeypatch, fake_client) -> None:
    monkeypatch.setattr(sync_mod, "JustCallClient", lambda cfg: fake_client)


def contact(
    contact_id: int,
    *,
    phone: str = "6145550100",
    status: str = "Active",
    created: str = "2026-07-01 10:00:00",
    fields: list[dict] | None = None,
) -> dict:
    return {
        "id": contact_id,
        "name": f"Contact {contact_id}",
        "phone_number": phone,
        "email": None,
        "status": status,
        "created_at": created,
        "custom_fields": fields
        if fields is not None
        else [{"key": "company_name", "label": "Company Name", "type": "text", "value": "Acme"}],
    }


# ------------------------------------------- discover_company_field (pure)


def test_discover_override_wins():
    assert discover_company_field([], "the_exact_key") == "the_exact_key"


def test_discover_unique_company_key():
    assert discover_company_field(load_fixture_contacts(), None) == "company_name"


def test_discover_business_key_when_no_company_key():
    contacts = [contact(1, fields=[{"key": "business_name", "label": "Business Name", "value": "X"}])]
    assert discover_company_field(contacts, None) == "business_name"


def test_discover_ambiguous_returns_none():
    contacts = [
        contact(
            1,
            fields=[
                {"key": "company_name", "label": "Company Name", "value": "A"},
                {"key": "parent_company", "label": "Parent Company", "value": "B"},
            ],
        )
    ]
    assert discover_company_field(contacts, None) is None


def test_discover_no_match_returns_none():
    contacts = [contact(1, fields=[{"key": "trade", "label": "Trade", "value": "Plumbing"}])]
    assert discover_company_field(contacts, None) is None


# ------------------------------------------------ extract_company_name (pure)


def test_extract_company_name_variants():
    row = contact(1, fields=[{"key": "company_name", "label": "Company Name", "value": "  Acme Drains  "}])
    assert extract_company_name(row, "company_name") == "Acme Drains"
    assert extract_company_name(row, "missing_key") is None
    assert extract_company_name(row, None) is None
    empty = contact(2, fields=[{"key": "company_name", "label": "Company Name", "value": ""}])
    assert extract_company_name(empty, "company_name") is None


# ----------------------------------------------------------------- run_sync


def test_run_sync_full_pass_over_fixtures(fake_db, monkeypatch):
    suppress_calls: list[tuple] = []
    install_fake_compliance(
        monkeypatch,
        suppress=lambda cfg, phone, reason, source: suppress_calls.append((phone, reason, source)) or True,
    )
    use_client(monkeypatch, fake_client_with({"Undialed": load_fixture_contacts()}))
    cfg = make_settings(consent_basis_first_touch="prior_express_invitation")

    stats = run_sync(cfg)

    assert (stats.fetched, stats.created, stats.updated) == (5, 5, 0)
    assert stats.company_field_key == "company_name"
    assert fake_db.kv["justcall.company_field"] == "company_name"

    rivera = fake_db.upserts[0][1]
    assert rivera["justcall_contact_id"] == 90210417
    assert rivera["phone_e164"] == "+16145550142"
    assert rivera["company_name"] == "Rivera Drain Pros"
    assert rivera["touch_type"] == "first"
    assert rivera["progress_status"] == "Undialed"
    assert rivera["consent_basis"] == "prior_express_invitation"
    assert rivera["custom_fields"].obj == load_fixture_contacts()[0]["custom_fields"]

    # Unparseable phone keeps the raw text so the row survives for the gate.
    earl = fake_db.upserts[2][1]
    assert earl["phone_e164"] == "555-01"

    # Empty company value → NULL, not "".
    okafor = fake_db.upserts[4][1]
    assert okafor["company_name"] is None

    # DNCA contact suppressed with normalized phone.
    assert suppress_calls == [("+18135550117", "dnca", "sync")]
    assert stats.suppressed == 1

    # Cursor = max created_at of the audience, stored as ISO UTC.
    assert fake_db.kv["justcall.sync_cursor.Undialed"] == "2026-07-21T14:09:52+00:00"

    # Consent basis is never overwritten on re-sync (SQL-level guarantee).
    assert "COALESCE(contacts.consent_basis, EXCLUDED.consent_basis)" in fake_db.upserts[0][0]


def test_run_sync_touch_mapping_and_consent(fake_db, monkeypatch):
    install_fake_compliance(monkeypatch)
    use_client(
        monkeypatch,
        fake_client_with({"Dialed": [contact(11)], "Skipped": [contact(12)]}),
    )
    cfg = make_settings(consent_basis_second_touch="prior rep relationship")

    run_sync(cfg)

    dialed = fake_db.upserts[0][1]
    assert dialed["touch_type"] == "second"
    assert dialed["progress_status"] == "Dialed"
    assert dialed["consent_basis"] == "prior rep relationship"

    skipped = fake_db.upserts[1][1]
    assert skipped["touch_type"] == "first"  # stored as first; planner filters
    assert skipped["progress_status"] == "Skipped"
    assert skipped["consent_basis"] is None  # first-touch consent unset → NULL


def test_run_sync_counts_updates(fake_db, monkeypatch):
    install_fake_compliance(monkeypatch)
    use_client(monkeypatch, fake_client_with({"Undialed": [contact(1), contact(2)]}))
    fake_db.insert_flags = [True, False]

    stats = run_sync(make_settings())

    assert (stats.created, stats.updated) == (1, 1)


def test_run_sync_reuses_stored_company_field(fake_db, monkeypatch):
    install_fake_compliance(monkeypatch)
    fake_db.kv["justcall.company_field"] = "biz"
    row = contact(1, fields=[{"key": "biz", "label": "whatever", "value": "B Corp"}])
    use_client(monkeypatch, fake_client_with({"Undialed": [row]}))

    stats = run_sync(make_settings())

    assert stats.company_field_key == "biz"
    assert fake_db.upserts[0][1]["company_name"] == "B Corp"


def test_run_sync_incremental_stops_at_cursor(fake_db, monkeypatch):
    install_fake_compliance(monkeypatch)
    fake_db.kv["justcall.sync_cursor.Undialed"] = "2026-07-15T00:00:00+00:00"
    overrun: list[str] = []

    def undialed(*, progress_status=None, contact_status=None, per_page=100):
        if progress_status != "Undialed":
            return
        yield contact(1, created="2026-07-20 10:00:00")   # newer than cursor
        yield contact(2, created="2026-07-10 10:00:00")   # older → early stop
        overrun.append("iterator advanced past the cursor break")
        yield contact(3, created="2026-07-01 10:00:00")

    use_client(monkeypatch, SimpleNamespace(fetch_campaign_contacts=undialed))
    # Pinned field key ⇒ no discovery buffering, so the stream stays lazy.
    cfg = make_settings(justcall_company_field_key="company_name")

    stats = run_sync(cfg)

    assert stats.fetched == 1
    assert overrun == []
    assert fake_db.kv["justcall.sync_cursor.Undialed"] == "2026-07-20T10:00:00+00:00"


def test_run_sync_full_ignores_cursor(fake_db, monkeypatch):
    install_fake_compliance(monkeypatch)
    fake_db.kv["justcall.sync_cursor.Undialed"] = "2026-07-15T00:00:00+00:00"
    rows = [contact(1, created="2026-07-20 10:00:00"), contact(2, created="2026-07-10 10:00:00")]
    use_client(monkeypatch, fake_client_with({"Undialed": rows}))
    cfg = make_settings(justcall_company_field_key="company_name")

    stats = run_sync(cfg, full=True)

    assert stats.fetched == 2


def test_run_sync_config_override_persisted(fake_db, monkeypatch):
    install_fake_compliance(monkeypatch)
    use_client(monkeypatch, fake_client_with({}))
    cfg = make_settings(justcall_company_field_key="pinned_key")

    stats = run_sync(cfg)

    assert stats.company_field_key == "pinned_key"
    assert fake_db.kv["justcall.company_field"] == "pinned_key"
