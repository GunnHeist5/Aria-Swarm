from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app import config, tasks, uploads
from app.db import control, tenant as tdb
from app.routes import templates
from app.security import Identity, require_client

router = APIRouter()


def _page_ctx(identity: Identity, conversation_id: str | None,
              watch: str | None = None):
    conversations = tdb.list_conversations(identity.tenant_id)
    messages = tdb.list_messages(identity.tenant_id, conversation_id) if conversation_id else []
    watch_analysis = None
    if watch:
        row = tdb.get_analysis(identity.tenant_id, watch)
        # only show the live card for an in-flight analysis; ignore bad ids
        if row and tasks.analysis_live_status(identity.tenant_id, row) in ("running", "awaiting_input"):
            watch_analysis = row
    return {
        "conversations": conversations,
        "conversation_id": conversation_id,
        "messages": messages,
        "watch_analysis": watch_analysis,
        "over_limit": control.tenant_over_limit(identity.tenant_id),
        "used": control.analyses_this_month(identity.tenant_id),
    }


@router.get("/chat", response_class=HTMLResponse)
def chat_page(request: Request, conversation: str | None = None, watch: str | None = None,
              identity: Identity = Depends(require_client)):
    return templates.TemplateResponse(request, "chat.html",
                                      _page_ctx(identity, conversation, watch))


@router.post("/chat")
def send(request: Request, question: str = Form(...), conversation: str | None = Form(None),
         identity: Identity = Depends(require_client)):
    tenant_id = identity.tenant_id
    if control.tenant_over_limit(tenant_id):
        ctx = _page_ctx(identity, conversation)
        ctx["error"] = "Monthly analysis limit reached — contact us to upgrade your plan."
        return templates.TemplateResponse(request, "chat.html", ctx, status_code=402)

    running = tdb.latest_running_analysis(tenant_id, conversation) if conversation else None
    if running and tasks.analysis_live_status(tenant_id, running) == "running":
        ctx = _page_ctx(identity, conversation, watch=running["analysis_id"])
        ctx["error"] = "An analysis is already running for this conversation — hang on."
        return templates.TemplateResponse(request, "chat.html", ctx, status_code=409)

    conversation_id = conversation or tdb.create_conversation(tenant_id, title=question[:60])
    analysis_id = tdb.create_analysis(tenant_id, question, conversation_id)
    control.audit(identity.key_id, "analysis_start", tenant_id=tenant_id, resource=analysis_id)
    tasks.spawn(tasks.run_analysis_task, tenant_id, question, conversation_id,
                analysis_id, identity.key_id)
    return RedirectResponse(f"/chat?conversation={conversation_id}&watch={analysis_id}",
                            status_code=303)


@router.post("/chat/ask")
async def ask(question: str = Form(...), conversation: str | None = Form(None),
              files: list[UploadFile] = File(default=[]),
              identity: Identity = Depends(require_client)):
    tenant_id = identity.tenant_id
    if control.tenant_over_limit(tenant_id):
        return JSONResponse({"error": "Monthly analysis limit reached — contact us to upgrade."},
                            status_code=402)
    running = tdb.latest_running_analysis(tenant_id, conversation) if conversation else None
    if running and tasks.analysis_live_status(tenant_id, running) in ("running", "awaiting_input"):
        return JSONResponse({"error": "An analysis is already running in this conversation."},
                            status_code=409)
    if len(files) > config.MAX_CHAT_FILES:
        return JSONResponse({"error": f"At most {config.MAX_CHAT_FILES} files per message."},
                            status_code=413)

    notes = []
    for f in files:
        data = await f.read()
        if not data:
            continue
        if len(data) > config.MAX_UPLOAD_BYTES:
            return JSONResponse({"error": f"{f.filename} is too large."}, status_code=413)
        result = uploads.ingest_upload(tenant_id, f.filename, data)
        control.audit(identity.key_id, "upload", tenant_id=tenant_id,
                      resource=result["id"], detail={"kind": result["kind"]})
        notes.append(result["note"])

    question = question.strip()
    if notes:
        question += "\n\n[The user attached: " + ", ".join(notes) + "]"

    conversation_id = conversation or tdb.create_conversation(tenant_id, title=question[:60])
    analysis_id = tdb.create_analysis(tenant_id, question, conversation_id)
    control.audit(identity.key_id, "analysis_start", tenant_id=tenant_id, resource=analysis_id)
    tasks.spawn(tasks.run_analysis_task, tenant_id, question, conversation_id,
                analysis_id, identity.key_id)
    return JSONResponse({"analysis_id": analysis_id, "conversation_id": conversation_id})


@router.post("/chat/analyses/{analysis_id}/answer")
def answer_question(analysis_id: str, question_id: str = Form(...), answer: str = Form(...),
                    identity: Identity = Depends(require_client)):
    tenant_id = identity.tenant_id
    row = tdb.get_analysis(tenant_id, analysis_id)
    if not row:
        raise HTTPException(status_code=404)
    q = tdb.get_analysis_question(tenant_id, question_id)
    if not q or q["analysis_id"] != analysis_id:
        raise HTTPException(status_code=404)
    if tdb.get_analysis_answer(tenant_id, question_id):
        return JSONResponse({"error": "already answered"}, status_code=409)
    tdb.add_analysis_answer(tenant_id, question_id, analysis_id, answer.strip()[:500])
    control.audit(identity.key_id, "question_answered", tenant_id=tenant_id, resource=question_id)
    return JSONResponse({"ok": True})


@router.get("/chat/analyses/{analysis_id}/events")
def analysis_events(analysis_id: str, after: int = 0,
                    identity: Identity = Depends(require_client)):
    # Resolved through THIS tenant's DB only — another tenant's id is a plain
    # 404, indistinguishable from a nonexistent one (deliverables pattern).
    row = tdb.get_analysis(identity.tenant_id, analysis_id)
    if not row:
        raise HTTPException(status_code=404)
    events = tdb.list_analysis_events(identity.tenant_id, analysis_id, after)
    status = tasks.analysis_live_status(identity.tenant_id, row)
    payload = {
        "events": [{"seq": e["id"], "kind": e["kind"], "text": e["text"]} for e in events],
        "status": status,
    }
    if status in ("running", "awaiting_input"):
        partial = tdb.get_analysis_partial(identity.tenant_id, analysis_id)
        if partial:
            payload["partial"] = partial
    if row["status"] == "done":
        payload["answer"] = row["summary"]
    return JSONResponse(payload)
