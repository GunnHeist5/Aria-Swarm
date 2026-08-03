from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from app import config
from app.db import control
from app.routes import templates
from app.security import Identity, require_admin

router = APIRouter(prefix="/admin")


def _ctx(new_key: str | None = None, new_tenant: str | None = None):
    tenants = control.list_tenants()
    for t in tenants:
        t["used"] = control.analyses_this_month(t["tenant_id"])
        t["limit"] = config.TIERS.get(t["tier"], {}).get("analyses_per_month")
    return {"tenants": tenants, "tiers": list(config.TIERS), "new_key": new_key, "new_tenant": new_tenant}


@router.get("", response_class=HTMLResponse)
def dashboard(request: Request, identity: Identity = Depends(require_admin)):
    return templates.TemplateResponse(request, "admin.html", _ctx())


@router.post("/tenants", response_class=HTMLResponse)
def create_tenant(request: Request, name: str = Form(...), tier: str = Form("starter"),
                  identity: Identity = Depends(require_admin)):
    tenant_id = control.create_tenant(name, tier)
    control.audit(identity.key_id, "tenant_created", tenant_id=tenant_id)
    return templates.TemplateResponse(request, "admin.html", _ctx(new_tenant=tenant_id))


@router.post("/keys", response_class=HTMLResponse)
def issue_key(request: Request, role: str = Form(...), tenant_id: str = Form(None),
              label: str = Form(None), identity: Identity = Depends(require_admin)):
    key_id, raw_key = control.issue_key(role, tenant_id or None, label)
    control.audit(identity.key_id, "key_issued", tenant_id=tenant_id or None, resource=key_id)
    # raw key rendered once, never stored
    return templates.TemplateResponse(request, "admin.html", _ctx(new_key=raw_key))
