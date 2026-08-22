"""Application entry point — FastAPI + web UI.

Mounts app.api.routes (Session 20) and app.web.base (Session 21) routers
into a single FastAPI application. Run with:

    uv run uvicorn app.main:app --reload
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.routes import router as api_router
from app.web.base import router as web_router

# Resolved from this file rather than the process working directory, so the
# app starts the same way regardless of where uvicorn was launched from.
_STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"

app = FastAPI(
    title="Email Marketing Tool",
    description="Self-hosted, local-first cold email platform",
    version="0.1.0",
)

# Mount the REST API (Session 20)
app.include_router(api_router)

# Mount the web UI (Session 21)
app.include_router(web_router)

# Static assets. Mounted unconditionally: the directory is committed, so a
# missing one is a packaging bug that should fail loudly at startup rather
# than leave every stylesheet 404ing with no explanation (CLAUDE.md 2.1).
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
