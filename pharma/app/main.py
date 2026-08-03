from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.db import control, knowledge
from app.routes import admin, auth, chat, data, deliverables, trainer


@asynccontextmanager
async def lifespan(app: FastAPI):
    control.connect().close()
    knowledge.connect().close()
    yield


app = FastAPI(title="Aria Pharma", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

app.mount("/static", StaticFiles(directory=Path(__file__).resolve().parent / "web" / "static"), name="static")

for router in (auth.router, chat.router, data.router, deliverables.router, trainer.router, admin.router):
    app.include_router(router)


@app.get("/")
def index():
    return RedirectResponse("/login")
