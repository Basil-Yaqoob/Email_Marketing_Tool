"""Application entry point — FastAPI + web UI.

Mounts app.api.routes (Session 20) and app.web.base (Session 21) routers
into a single FastAPI application. Run with:

    uv run uvicorn app.main:app --reload
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.routes import router as api_router
from app.web.base import router as web_router

app = FastAPI(
    title="Email Marketing Tool",
    description="Self-hosted, local-first cold email platform",
    version="0.1.0",
)

# Mount the REST API (Session 20)
app.include_router(api_router)

# Mount the web UI (Session 21)
app.include_router(web_router)

# Serve static assets if they exist
try:
    app.mount("/static", StaticFiles(directory="app/web/static"), name="static")
except RuntimeError:
    # static/ directory doesn't exist yet; that's fine
    pass
