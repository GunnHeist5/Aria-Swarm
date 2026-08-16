from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app import tasks
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
        # only show the live card for an actually-running analysis; ignore bad ids
        if row and tasks.analysis_live_status(identity.tenant_id, row) == "running":
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


@router.get("/chat/analyses/{analysis_id}/events")
def analysis_events(analysis_id: str, after: int = 0,
                    identity: Identity = Depends(require_client)):
    # Resolved through THIS tenant's DB only — another tenant's id is a plain
    # 404, indistinguishable from a nonexistent one (deliverables pattern).
    row = tdb.get_analysis(identity.tenant_id, analysis_id)
    if not row:
        raise HTTPException(status_code=404)
    events = tdb.list_analysis_events(identity.tenant_id, analysis_id, after)
    payload = {
        "events": [{"seq": e["id"], "kind": e["kind"], "text": e["text"]} for e in events],
        "status": tasks.analysis_live_status(identity.tenant_id, row),
    }
    if row["status"] == "done":
        payload["answer"] = row["summary"]
    return JSONResponse(payload)
