from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import config, security
from app.db import control
from app.routes import templates

router = APIRouter()

_LANDING = {"client": "/chat", "trainer": "/trainer", "admin": "/admin"}


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
def login(request: Request, token: str = Form(...)):
    row = control.resolve_key(token.strip())
    if row is None:
        control.audit("anonymous", "login_failed")
        return templates.TemplateResponse(request, "login.html", {"error": "Invalid token."}, status_code=401)
    control.audit(row["key_id"], "login", tenant_id=row["tenant_id"])
    response = RedirectResponse(_LANDING.get(row["role"], "/chat"), status_code=303)
    response.set_cookie(
        config.SESSION_COOKIE,
        security.make_session_cookie(token.strip()),
        max_age=config.SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
    )
    return response


@router.post("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(config.SESSION_COOKIE)
    return response
