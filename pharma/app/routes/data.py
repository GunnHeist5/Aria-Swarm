from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from app import config, uploads
from app.db import control, tenant as tdb
from app.routes import templates
from app.security import Identity, require_client

router = APIRouter()


@router.get("/datasets", response_class=HTMLResponse)
def datasets_page(request: Request, identity: Identity = Depends(require_client)):
    return templates.TemplateResponse(
        request, "datasets.html",
        {"datasets": tdb.list_datasets(identity.tenant_id), "error": None},
    )


@router.post("/datasets/upload")
async def upload(request: Request, file: UploadFile, identity: Identity = Depends(require_client)):
    data = await file.read()
    if len(data) > config.MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413)

    filename = uploads.sanitize_filename(file.filename, "upload.csv")
    if not filename.lower().endswith(uploads.DATASET_EXTS):
        return templates.TemplateResponse(
            request, "datasets.html",
            {"datasets": tdb.list_datasets(identity.tenant_id),
             "error": "Only .csv and .xlsx files are accepted here — attach other files in the chat."},
            status_code=400,
        )

    result = uploads.ingest_upload(identity.tenant_id, filename, data)
    control.audit(identity.key_id, "upload", tenant_id=identity.tenant_id,
                  resource=result["id"], detail={"rows": result.get("rows")})
    return RedirectResponse("/datasets", status_code=303)
