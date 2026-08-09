"""Suppression ledger + attempt counting, with orchestrator.db monkeypatched.

These modules are thin repos; what matters is that they normalize phones
before touching SQL, fail toward blocking, and report already-present rows.
"""

from __future__ import annotations

import pytest

import orchestrator.db
from orchestrator.compliance import attempts, suppression


class FakeDb:
    """Captures SQL/params; canned answers per table."""

    def __init__(self):
        self.suppressed: set[str] = set()
        self.attempt_counts: dict[str, int] = {}
        self.executed: list[tuple[str, tuple]] = []
        self.execute_rowcount = 1

    def query_one(self, cfg, sql, params=None):
        if "FROM suppression" in sql:
            return {"present": 1} if params[0] in self.suppressed else None
        if "FROM call_attempts" in sql:
            return {"n": self.attempt_counts.get(params[0], 0)}
        raise AssertionError(f"unexpected query: {sql}")

    def execute(self, cfg, sql, params=None):
        self.executed.append((sql, params))
        return self.execute_rowcount


@pytest.fixture
def fakedb(monkeypatch) -> FakeDb:
    fake = FakeDb()
    monkeypatch.setattr(orchestrator.db, "query_one", fake.query_one)
    monkeypatch.setattr(orchestrator.db, "execute", fake.execute)
    return fake


# --- suppression ------------------------------------------------------------

def test_is_suppressed(cfg, fakedb):
    fakedb.suppressed.add("+16145550100")
    assert suppression.is_suppressed(cfg, "+16145550100")
    assert not suppression.is_suppressed(cfg, "+16145550199")


def test_is_suppressed_normalizes_before_lookup(cfg, fakedb):
    fakedb.suppressed.add("+16145550100")
    # A formatted opt-out and an E.164 dial must meet in the same row.
    assert suppression.is_suppressed(cfg, "(614) 555-0100")
    assert suppression.is_suppressed(cfg, "1-614-555-0100")


def test_suppress_inserts_normalized_phone(cfg, fakedb):
    assert suppression.suppress(cfg, "(614) 555-0100", "opt_out", "webhook:123")
    sql, params = fakedb.executed[0]
    assert "INSERT INTO suppression" in sql
    assert "ON CONFLICT" in sql and "DO NOTHING" in sql
    assert params == ("+16145550100", "opt_out", "webhook:123")


def test_suppress_returns_false_when_already_present(cfg, fakedb):
    fakedb.execute_rowcount = 0  # ON CONFLICT DO NOTHING inserted nothing
    assert suppression.suppress(cfg, "+16145550100", "opt_out", "cli") is False


def test_suppress_keeps_unparseable_input_verbatim(cfg, fakedb):
    # A malformed opt-out must still land in the ledger, not be dropped.
    assert suppression.suppress(cfg, "ext. 4411??", "opt_out", "manual")
    _sql, params = fakedb.executed[0]
    assert params[0] == "ext. 4411??"


# --- attempts ---------------------------------------------------------------

def test_attempts_for_phone_counts(cfg, fakedb):
    fakedb.attempt_counts["+16145550100"] = 3
    assert attempts.attempts_for_phone(cfg, "+16145550100") == 3
    assert attempts.attempts_for_phone(cfg, "+16145550199") == 0


def test_attempts_for_phone_normalizes(cfg, fakedb):
    fakedb.attempt_counts["+16145550100"] = 2
    assert attempts.attempts_for_phone(cfg, "614-555-0100") == 2


def test_attempts_excludes_only_canceled(cfg, fakedb, monkeypatch):
    # The cap must count orphaned/failed dials (we can't prove they didn't
    # ring), excluding only rows that never could have reached the phone.
    captured: dict[str, str] = {}

    def spy(cfg_, sql, params=None):
        captured["sql"] = sql
        return {"n": 0}

    monkeypatch.setattr(orchestrator.db, "query_one", spy)
    attempts.attempts_for_phone(cfg, "+16145550100")
    assert "status <> 'canceled'" in captured["sql"]
    assert "orphaned" not in captured["sql"]
