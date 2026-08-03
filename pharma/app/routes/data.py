import hashlib
import io
import json
import re

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from app import config
from app.db import control, tenant as tdb
from app.routes import templates
from app.security import Identity, require_client

router = APIRouter()

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


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

    filename = _SAFE_NAME_RE.sub("_", file.filename or "upload.csv").lstrip(".")
    if not filename.lower().endswith((".csv", ".xlsx")):
        return templates.TemplateResponse(
            request, "datasets.html",
            {"datasets": tdb.list_datasets(identity.tenant_id),
             "error": "Only .csv and .xlsx files are accepted."},
            status_code=400,
        )

    rows = None
    schema_json = None
    try:
        import pandas as pd

        df = pd.read_csv(io.BytesIO(data)) if filename.lower().endswith(".csv") \
            else pd.read_excel(io.BytesIO(data))
        rows = len(df)
        schema_json = json.dumps({c: str(t) for c, t in df.dtypes.items()})
    except Exception:
        pass  # store anyway; the agent can inspect it

    dest = tdb.resolve_tenant_file(identity.tenant_id, f"uploads/{filename}")
    dest.write_bytes(data)
    dataset_id = tdb.add_dataset(
        identity.tenant_id, filename, f"uploads/{filename}",
        hashlib.sha256(data).hexdigest(), rows, schema_json,
    )
    control.audit(identity.key_id, "upload", tenant_id=identity.tenant_id,
                  resource=dataset_id, detail={"rows": rows})
    return RedirectResponse("/datasets", status_code=303)
