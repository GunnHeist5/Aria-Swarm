from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from app.agent import analyst
from app.db import control, tenant as tdb
from app.routes import templates
from app.security import Identity, require_client

router = APIRouter()


def _page_ctx(identity: Identity, conversation_id: str | None):
    conversations = tdb.list_conversations(identity.tenant_id)
    messages = tdb.list_messages(identity.tenant_id, conversation_id) if conversation_id else []
    return {
        "conversations": conversations,
        "conversation_id": conversation_id,
        "messages": messages,
        "over_limit": control.tenant_over_limit(identity.tenant_id),
        "used": control.analyses_this_month(identity.tenant_id),
    }


@router.get("/chat", response_class=HTMLResponse)
def chat_page(request: Request, conversation: str | None = None,
              identity: Identity = Depends(require_client)):
    return templates.TemplateResponse(request, "chat.html", _page_ctx(identity, conversation))


@router.post("/chat", response_class=HTMLResponse)
def send(request: Request, question: str = Form(...), conversation: str | None = Form(None),
         identity: Identity = Depends(require_client)):
    tenant_id = identity.tenant_id
    if control.tenant_over_limit(tenant_id):
        ctx = _page_ctx(identity, conversation)
        ctx["error"] = "Monthly analysis limit reached — contact us to upgrade your plan."
        return templates.TemplateResponse(request, "chat.html", ctx, status_code=402)

    conversation_id = conversation or tdb.create_conversation(tenant_id, title=question[:60])
    control.audit(identity.key_id, "analysis_start", tenant_id=tenant_id, resource=conversation_id)
    result = analyst.run_analysis(tenant_id, question, conversation_id)
    control.audit(identity.key_id, "analysis_finish", tenant_id=tenant_id,
                  resource=result.analysis_id, detail={"status": result.status})
    ctx = _page_ctx(identity, conversation_id)
    if result.status != "done":
        ctx["error"] = f"Analysis {result.status}."
    return templates.TemplateResponse(request, "chat.html", ctx)
