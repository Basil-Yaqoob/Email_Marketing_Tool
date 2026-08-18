"""Web UI base route and template configuration."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

router = APIRouter(tags=["web"])

# Jinja2 template configuration with autoescaping on
templates = Jinja2Templates(directory="app/web/templates", autoescape=True)


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> str:
    """Homepage redirects to dashboard."""
    return templates.TemplateResponse("index.html", {"request": request})


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request) -> str:
    """Main dashboard with alerts, campaigns, and analytics."""
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "alerts": [],  # Stub: real implementation queries DB
            "campaigns": [],
        },
    )


@router.get("/campaigns/new", response_class=HTMLResponse)
async def campaign_wizard_start(request: Request) -> str:
    """Campaign creation wizard (step 1: name)."""
    return templates.TemplateResponse("wizard/step1_name.html", {"request": request})


@router.post("/campaigns", response_class=HTMLResponse)
async def campaign_wizard_create(request: Request) -> str:
    """Create campaign and proceed to ICP step."""
    # Stub: real implementation validates and creates
    return templates.TemplateResponse("wizard/step2_icp.html", {"request": request})


@router.get("/campaigns/{campaign_id}/leads", response_class=HTMLResponse)
async def leads_table(request: Request, campaign_id: str) -> str:
    """Lead table with filtering and pagination."""
    return templates.TemplateResponse(
        "leads/table.html",
        {
            "request": request,
            "campaign_id": campaign_id,
            "leads": [],
            "total": 0,
            "page": 1,
        },
    )


@router.get("/campaigns/{campaign_id}/leads/{lead_id}/expand", response_class=HTMLResponse)
async def lead_expand(request: Request, campaign_id: str, lead_id: str) -> str:
    """HTMX fragment: expand lead row to show provenance."""
    return templates.TemplateResponse(
        "leads/expand.html",
        {
            "request": request,
            "lead": {},  # Stub: real implementation queries DB
        },
    )


@router.get("/campaigns/{campaign_id}/messages", response_class=HTMLResponse)
async def messages_review(request: Request, campaign_id: str) -> str:
    """Message review queue."""
    return templates.TemplateResponse(
        "messages/review.html",
        {
            "request": request,
            "campaign_id": campaign_id,
            "messages": [],
        },
    )


@router.get("/mailboxes", response_class=HTMLResponse)
async def mailboxes_list(request: Request) -> str:
    """Mailbox health cards."""
    return templates.TemplateResponse(
        "mailboxes/list.html",
        {
            "request": request,
            "mailboxes": [],
        },
    )


@router.get("/analytics/{campaign_id}", response_class=HTMLResponse)
async def analytics_report(request: Request, campaign_id: str) -> str:
    """Campaign analytics with funnel, yield, and alerts."""
    return templates.TemplateResponse(
        "analytics/report.html",
        {
            "request": request,
            "campaign_id": campaign_id,
        },
    )


@router.get("/jobs/{job_id}/progress", response_class=HTMLResponse)
async def job_progress_fragment(request: Request, job_id: str) -> str:
    """HTMX fragment: live job progress (polled every 2s)."""
    return templates.TemplateResponse(
        "jobs/progress_fragment.html",
        {
            "request": request,
            "job_id": job_id,
        },
    )


__all__ = ["router", "templates"]
