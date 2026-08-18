"""Web UI base route and template configuration."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader, select_autoescape

router = APIRouter(tags=["web"])

# Jinja2 template configuration with autoescaping on. Starlette's
# Jinja2Templates no longer takes `autoescape` directly (fixed while
# closing out `make check` for Sessions 22-23 -- this was a pre-existing
# mypy failure from Session 21: the kwarg silently did nothing at runtime
# since it doesn't match either overload, so autoescaping was only ever on
# by Jinja2's own default for "html"/"xml" extensions via select_autoescape
# below, not because the removed kwarg asked for it).
templates = Jinja2Templates(
    env=Environment(
        loader=FileSystemLoader("app/web/templates"),
        autoescape=select_autoescape(["html", "xml"]),
    )
)


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    """Homepage redirects to dashboard."""
    return templates.TemplateResponse(request, "index.html")


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    """Main dashboard with alerts, campaigns, and analytics."""
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "alerts": [],  # Stub: real implementation queries DB
            "campaigns": [],
        },
    )


@router.get("/campaigns/new", response_class=HTMLResponse)
async def campaign_wizard_start(request: Request) -> HTMLResponse:
    """Campaign creation wizard (step 1: name)."""
    return templates.TemplateResponse(request, "wizard/step1_name.html")


@router.post("/campaigns", response_class=HTMLResponse)
async def campaign_wizard_create(request: Request) -> HTMLResponse:
    """Create campaign and proceed to ICP step."""
    # Stub: real implementation validates and creates
    return templates.TemplateResponse(request, "wizard/step2_icp.html")


@router.get("/campaigns/{campaign_id}/leads", response_class=HTMLResponse)
async def leads_table(request: Request, campaign_id: str) -> HTMLResponse:
    """Lead table with filtering and pagination."""
    return templates.TemplateResponse(
        request,
        "leads/table.html",
        {
            "campaign_id": campaign_id,
            "leads": [],
            "total": 0,
            "page": 1,
        },
    )


@router.get("/campaigns/{campaign_id}/leads/{lead_id}/expand", response_class=HTMLResponse)
async def lead_expand(request: Request, campaign_id: str, lead_id: str) -> HTMLResponse:
    """HTMX fragment: expand lead row to show provenance."""
    return templates.TemplateResponse(
        request,
        "leads/expand.html",
        {
            "lead": {},  # Stub: real implementation queries DB
        },
    )


@router.get("/campaigns/{campaign_id}/messages", response_class=HTMLResponse)
async def messages_review(request: Request, campaign_id: str) -> HTMLResponse:
    """Message review queue."""
    return templates.TemplateResponse(
        request,
        "messages/review.html",
        {
            "campaign_id": campaign_id,
            "messages": [],
        },
    )


@router.get("/mailboxes", response_class=HTMLResponse)
async def mailboxes_list(request: Request) -> HTMLResponse:
    """Mailbox health cards."""
    return templates.TemplateResponse(
        request,
        "mailboxes/list.html",
        {
            "mailboxes": [],
        },
    )


@router.get("/analytics/{campaign_id}", response_class=HTMLResponse)
async def analytics_report(request: Request, campaign_id: str) -> HTMLResponse:
    """Campaign analytics with funnel, yield, and alerts."""
    return templates.TemplateResponse(
        request,
        "analytics/report.html",
        {
            "campaign_id": campaign_id,
        },
    )


@router.get("/jobs/{job_id}/progress", response_class=HTMLResponse)
async def job_progress_fragment(request: Request, job_id: str) -> HTMLResponse:
    """HTMX fragment: live job progress (polled every 2s)."""
    return templates.TemplateResponse(
        request,
        "jobs/progress_fragment.html",
        {
            "job_id": job_id,
        },
    )


__all__ = ["router", "templates"]
