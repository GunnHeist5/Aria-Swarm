import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Every test gets a fresh data root and a configured secret. No test may
    touch the real var/ or the network."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "var")
    monkeypatch.setattr(config, "SECRET_KEY", "test-secret")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    yield


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as tc:
        yield tc


@pytest.fixture
def demo_tenant():
    from app.db import control

    tenant_id = control.create_tenant("Test Pharma A")
    _, raw_key = control.issue_key("client", tenant_id, "test")
    return tenant_id, raw_key
