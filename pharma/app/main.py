from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.db import control, knowledge
from app.routes import admin, auth, chat, data, deliverables, gym, trainer


@asynccontextmanager
async def lifespan(app: FastAPI):
    control.connect().close()
    knowledge.connect().close()
    yield


app = FastAPI(title="Aria Pharma", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

app.mount("/static", StaticFiles(directory=Path(__file__).resolve().parent / "web" / "static"), name="static")

for router in (auth.router, chat.router, data.router, deliverables.router, trainer.router, gym.router, admin.router):
    app.include_router(router)


@app.exception_handler(HTTPException)
async def unauthenticated_to_login(request: Request, exc: HTTPException):
    """A browser (Accept: text/html) hitting a protected page while signed out
    gets the sign-in page, not a bare 401. API/polling callers still get the
    plain 401 so their error handling keeps working."""
    if exc.status_code == 401 and "text/html" in request.headers.get("accept", ""):
        return RedirectResponse("/login", status_code=303)
    return await http_exception_handler(request, exc)


@app.get("/")
def index():
    return RedirectResponse("/login")
