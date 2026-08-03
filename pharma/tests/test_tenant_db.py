import pytest

from app.db import control, tenant as tdb


def test_invalid_tenant_ids_refused():
    for bad in ("", "t_../../../etc", "t_ABCDEF123456", "t_short", "x_123456789abc", None, 42):
        with pytest.raises(PermissionError):
            tdb.validate_tenant_id(bad)


def test_unknown_but_wellformed_tenant_refused():
    with pytest.raises(PermissionError):
        tdb.validate_tenant_id("t_0123456789ab")


def test_path_escape_refused():
    tenant_id = control.create_tenant("Escape Co")
    with pytest.raises(PermissionError):
        tdb.resolve_tenant_file(tenant_id, "../other-tenant/tenant.db")
    with pytest.raises(PermissionError):
        tdb.resolve_tenant_file(tenant_id, "uploads/../../control.db")


def test_deliverable_write_refuses_escaping_path():
    tenant_id = control.create_tenant("Escape Co 2")
    with pytest.raises(PermissionError):
        tdb.add_deliverable(tenant_id, None, "chart", "x", "../../secrets.png", "image/png")


def test_tenant_dirs_are_0700():
    tenant_id = control.create_tenant("Perms Co")
    root = tdb.tenant_dir(tenant_id)
    assert (root.stat().st_mode & 0o777) == 0o700


def test_correction_review_lands_in_learnings():
    tenant_id = control.create_tenant("Learn Co")
    analysis_id = tdb.create_analysis(tenant_id, "q")
    tdb.add_review(tenant_id, analysis_id, "trainer-1", "correct", "Always normalize TRx by market growth")
    learnings = tdb.recent_learnings(tenant_id)
    assert any("normalize TRx" in l["text"] for l in learnings)
