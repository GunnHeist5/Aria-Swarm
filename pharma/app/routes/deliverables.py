from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse

from app.db import control, tenant as tdb
from app.routes import templates
from app.security import Identity, require_client

router = APIRouter()


@router.get("/deliverables", response_class=HTMLResponse)
def gallery(request: Request, identity: Identity = Depends(require_client)):
    return templates.TemplateResponse(
        request, "deliverables.html",
        {"deliverables": tdb.list_deliverables(identity.tenant_id),
         "analyses": tdb.list_analyses(identity.tenant_id)},
    )


@router.get("/deliverables/{deliverable_id}/download")
def download(deliverable_id: str, identity: Identity = Depends(require_client)):
    # Resolution goes through THIS tenant's DB only: a valid id belonging to
    # another tenant is simply not found here -> 404, indistinguishable from
    # a nonexistent id.
    row = tdb.get_deliverable(identity.tenant_id, deliverable_id)
    if not row:
        raise HTTPException(status_code=404)
    path = tdb.resolve_tenant_file(identity.tenant_id, row["stored_path"])
    if not path.is_file():
        raise HTTPException(status_code=404)
    control.audit(identity.key_id, "download", tenant_id=identity.tenant_id, resource=deliverable_id)
    return FileResponse(path, media_type=row["mime"] or "application/octet-stream",
                        filename=path.name)
