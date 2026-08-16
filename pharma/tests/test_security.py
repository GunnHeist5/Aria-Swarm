"""Isolation and auth tests — the first-class citizens of this suite."""

from app.db import control, tenant as tdb


def _auth(raw_key: str) -> dict:
    return {"Authorization": f"Bearer {raw_key}"}


def test_unknown_token_is_401(client):
    assert client.get("/datasets", headers=_auth("pk_client_bogus")).status_code == 401


def test_missing_token_is_401(client):
    assert client.get("/datasets").status_code == 401


def test_revoked_key_is_401(client, demo_tenant):
    tenant_id, raw_key = demo_tenant
    assert client.get("/datasets", headers=_auth(raw_key)).status_code == 200
    with control.connect() as conn:
        key_id = conn.execute("SELECT key_id FROM api_keys").fetchone()["key_id"]
    control.revoke_key(key_id)
    assert client.get("/datasets", headers=_auth(raw_key)).status_code == 401


def test_suspended_tenant_locks_out(client, demo_tenant):
    tenant_id, raw_key = demo_tenant
    with control.connect() as conn:
        conn.execute("UPDATE tenants SET status = 'suspended' WHERE tenant_id = ?", (tenant_id,))
    assert client.get("/datasets", headers=_auth(raw_key)).status_code == 401


def test_client_cannot_reach_staff_surfaces(client, demo_tenant):
    _, raw_key = demo_tenant
    assert client.get("/trainer", headers=_auth(raw_key)).status_code == 403
    assert client.get("/admin", headers=_auth(raw_key)).status_code == 403


def test_trainer_cannot_reach_admin(client):
    _, trainer_key = control.issue_key("trainer", None, "t")
    assert client.get("/admin", headers=_auth(trainer_key)).status_code == 403
    assert client.get("/trainer", headers=_auth(trainer_key)).status_code == 200


def test_tenant_a_cannot_download_tenant_b_deliverable(client):
    """The core cross-tenant leak test: a real deliverable id from tenant B is
    a plain 404 for tenant A — indistinguishable from a nonexistent id."""
    tenant_a = control.create_tenant("A Corp")
    tenant_b = control.create_tenant("B Corp")
    _, key_a = control.issue_key("client", tenant_a)

    secret = tdb.tenant_dir(tenant_b) / "deliverables" / "secret.png"
    secret.write_bytes(b"tenant B confidential chart")
    deliverable_b = tdb.add_deliverable(tenant_b, None, "chart", "B secret", "deliverables/secret.png", "image/png")

    response = client.get(f"/deliverables/{deliverable_b}/download", headers=_auth(key_a))
    assert response.status_code == 404
    assert b"confidential" not in response.content


def test_client_key_requires_existing_tenant():
    import pytest

    with pytest.raises(ValueError):
        control.issue_key("client", "t_000000000000")
    with pytest.raises(ValueError):
        control.issue_key("client", None)


def test_staff_keys_must_not_be_tenant_bound(demo_tenant):
    import pytest

    tenant_id, _ = demo_tenant
    with pytest.raises(ValueError):
        control.issue_key("trainer", tenant_id)


def test_raw_keys_never_stored(demo_tenant):
    _, raw_key = demo_tenant
    with control.connect() as conn:
        rows = conn.execute("SELECT key_hash FROM api_keys").fetchall()
    assert all(raw_key not in r["key_hash"] for r in rows)


def test_session_cookie_secure_flag_follows_config(client, demo_tenant, monkeypatch):
    from app import config

    _, raw_key = demo_tenant
    r = client.post("/login", data={"token": raw_key}, follow_redirects=False)
    assert "secure" not in r.headers["set-cookie"].lower()

    monkeypatch.setattr(config, "COOKIE_SECURE", True)
    r = client.post("/login", data={"token": raw_key}, follow_redirects=False)
    assert "secure" in r.headers["set-cookie"].lower()
