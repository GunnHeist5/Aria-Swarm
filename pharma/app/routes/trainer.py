import json

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from app.db import control, knowledge, tenant as tdb
from app.routes import templates
from app.security import Identity, require_trainer
from app.training import ingest, review

router = APIRouter(prefix="/trainer")


@router.get("", response_class=HTMLResponse)
def console(request: Request, identity: Identity = Depends(require_trainer)):
    methods = knowledge.list_methods()
    for m in methods:
        m["flags"] = json.loads(m["scan_flags"]) if m["scan_flags"] else []
    return templates.TemplateResponse(
        request, "trainer.html",
        {"methods": methods, "tenants": control.list_tenants()},
    )


@router.post("/ingest")
async def upload_doc(file: UploadFile, domain: str = Form("general"),
                     identity: Identity = Depends(require_trainer)):
    data = await file.read()
    method_id = ingest.ingest_document(file.filename or "document", data, domain=domain)
    control.audit(identity.key_id, "method_ingest", resource=method_id)
    return RedirectResponse("/trainer", status_code=303)


@router.post("/methods/{method_id}/scan")
def run_scan(method_id: str, identity: Identity = Depends(require_trainer)):
    review.scan(method_id)
    control.audit(identity.key_id, "method_scan", resource=method_id)
    return RedirectResponse("/trainer", status_code=303)


@router.post("/methods/{method_id}/edit")
def edit(method_id: str, title: str = Form(...), body_md: str = Form(...),
         identity: Identity = Depends(require_trainer)):
    knowledge.update_draft(method_id, title=title, body_md=body_md)
    control.audit(identity.key_id, "method_edit", resource=method_id)
    return RedirectResponse("/trainer", status_code=303)


@router.post("/methods/{method_id}/approve")
def approve(method_id: str, anonymized: str = Form(None),
            identity: Identity = Depends(require_trainer)):
    if anonymized != "yes":
        raise HTTPException(status_code=400, detail="anonymization must be explicitly confirmed")
    try:
        review.approve_method(method_id, identity.key_id, anonymization_confirmed=True)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    control.audit(identity.key_id, "method_approve", resource=method_id)
    return RedirectResponse("/trainer", status_code=303)


@router.post("/methods/{method_id}/reject")
def reject(method_id: str, identity: Identity = Depends(require_trainer)):
    review.reject_method(method_id, identity.key_id)
    control.audit(identity.key_id, "method_reject", resource=method_id)
    return RedirectResponse("/trainer", status_code=303)


@router.post("/corrections")
def file_correction(tenant_id: str = Form(...), analysis_id: str = Form(...),
                    correction: str = Form(...), identity: Identity = Depends(require_trainer)):
    """A correction lands in THAT client's Layer-2 learnings, not the library."""
    tdb.add_review(tenant_id, analysis_id, identity.key_id, "correct", correction)
    control.audit(identity.key_id, "correction_filed", tenant_id=tenant_id, resource=analysis_id)
    return RedirectResponse("/trainer", status_code=303)


@router.post("/promote")
def promote(tenant_id: str = Form(...), title: str = Form(...), body_md: str = Form(...),
            domain: str = Form("general"), identity: Identity = Depends(require_trainer)):
    """Explicit Layer-2 -> Layer-1 promotion: trainer writes the generalized
    version; it enters as a draft and still passes scan + approval."""
    method_id = review.promote_learning(tenant_id, "", title, body_md, domain)
    control.audit(identity.key_id, "learning_promoted", resource=method_id)
    return RedirectResponse("/trainer", status_code=303)
