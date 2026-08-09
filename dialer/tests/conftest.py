"""Shared test fixtures.

Tests must run with no network and no live Postgres/Redis. `make_settings`
builds a Settings with harmless defaults; pass overrides for the knob under
test. Nothing here reads the developer's real .env (env_file is disabled).
"""

from __future__ import annotations

import pytest

from orchestrator.config import Settings


def make_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "_env_file": None,  # never pick up a developer's real .env in tests
        "database_url": "postgresql://test:test@localhost:1/test",
        "redis_url": "redis://localhost:1/9",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


@pytest.fixture
def cfg() -> Settings:
    return make_settings()
