"""
FastAPI entry point (production).

    uvicorn backend.app:app --host 0.0.0.0 --port 8000

Serves the API under /api/* and the static dashboard from /frontend.
For a zero-dependency local run, use `python -m backend.devserver` instead.
"""
from __future__ import annotations

import os

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .core import App

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")

core = App()
app = FastAPI(title="AskDash", version="1.0", docs_url="/api/docs", redoc_url=None)


class AskBody(BaseModel):
    request: str = Field(..., max_length=500)
    panels: list[dict] = []
    history: list[dict] = []


class QueryBody(BaseModel):
    sql: str = Field(..., max_length=4000)


def _client_id(req: Request) -> str:
    fwd = req.headers.get("x-forwarded-for")
    return (fwd.split(",")[0].strip() if fwd else (req.client.host if req.client else "anon"))


@app.get("/api/health")
def health():
    return {"ok": True, "planner": core.planner.name}


@app.get("/api/schema")
def schema():
    return core.schema()


@app.get("/api/dashboard")
def dashboard():
    return core.dashboard()


@app.post("/api/query")
def query(body: QueryBody):
    return core.query(body.sql)


@app.post("/api/ask")
def ask(body: AskBody, req: Request):
    return core.ask(body.request, body.panels, body.history, client_id=_client_id(req))


@app.get("/")
def index():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


app.mount("/", StaticFiles(directory=FRONTEND_DIR), name="static")
