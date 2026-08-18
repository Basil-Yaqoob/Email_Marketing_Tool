"""REST API routes for campaigns, leads, messages, mailboxes, and analytics.

All pipeline work returns 202 Accepted with a job id. Request/response models
only; SQLAlchemy models never cross the boundary.
"""

# ruff: noqa: B008
from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import InvalidTokenError, verify_bearer_token
from app.api.models import (
    AlertResponse,
    AnalyticsReport,
    CampaignCreate,
    CampaignResponse,
    CampaignUpdate,
    JobStatus,
    LeadResponse,
    LeadsListResponse,
    MailboxResponse,
    MessageResponse,
    SuppressionAdd,
)
from app.core.config import Settings
from app.core.errors import MissingConfigError

if TYPE_CHECKING:
    pass

router = APIRouter(prefix="/api/v1", tags=["main"])


def _load_settings() -> Settings:
    return Settings()


# Dependency: Bearer token auth
async def verify_auth(
    authorization: str | None = Header(None),
    settings: Settings = Depends(_load_settings),
) -> None:
    """Verify bearer token using constant-time comparison.

    The unsubscribe endpoint is public; all others require auth. Delegates
    to app.api.auth.verify_bearer_token, the same check the MCP server's
    Streamable HTTP transport uses (Session 23) -- one mechanism, two
    callers.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid token")

    token = authorization[7:]  # Strip "Bearer "
    configured = settings.api_token.get_secret_value() if settings.api_token else None

    try:
        verify_bearer_token(token, configured=configured)
    except MissingConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


# ============================================================================
# Campaigns
# ============================================================================


@router.post("/campaigns", status_code=201)
async def create_campaign(
    req: CampaignCreate,
    session: AsyncSession = Depends(),
    auth: None = Depends(verify_auth),
) -> CampaignResponse:
    """Create a campaign.

    Validates ICP before persisting.
    """
    # Stub: real implementation validates ICP and creates campaign record
    raise HTTPException(status_code=501, detail="Not implemented")


@router.get("/campaigns")
async def list_campaigns(
    session: AsyncSession = Depends(),
    auth: None = Depends(verify_auth),
) -> list[CampaignResponse]:
    """List all campaigns."""
    # Stub
    raise HTTPException(status_code=501, detail="Not implemented")


@router.get("/campaigns/{campaign_id}")
async def get_campaign(
    campaign_id: UUID,
    session: AsyncSession = Depends(),
    auth: None = Depends(verify_auth),
) -> CampaignResponse:
    """Get campaign detail with funnel counts."""
    # Stub
    raise HTTPException(status_code=501, detail="Not implemented")


@router.patch("/campaigns/{campaign_id}")
async def update_campaign(
    campaign_id: UUID,
    req: CampaignUpdate,
    session: AsyncSession = Depends(),
    auth: None = Depends(verify_auth),
) -> CampaignResponse:
    """Update campaign ICP or settings."""
    # Stub
    raise HTTPException(status_code=501, detail="Not implemented")


@router.post("/campaigns/{campaign_id}/discover", status_code=202)
async def trigger_discovery(
    campaign_id: UUID,
    session: AsyncSession = Depends(),
    auth: None = Depends(verify_auth),
) -> JobStatus:
    """Trigger company discovery. Returns job id (non-blocking)."""
    # Stub
    raise HTTPException(status_code=501, detail="Not implemented")


@router.post("/campaigns/{campaign_id}/enrich", status_code=202)
async def trigger_enrichment(
    campaign_id: UUID,
    session: AsyncSession = Depends(),
    auth: None = Depends(verify_auth),
) -> JobStatus:
    """Trigger enrichment (person, email, verification). Returns job id."""
    # Stub
    raise HTTPException(status_code=501, detail="Not implemented")


@router.post("/campaigns/{campaign_id}/research", status_code=202)
async def trigger_hook_research(
    campaign_id: UUID,
    session: AsyncSession = Depends(),
    auth: None = Depends(verify_auth),
) -> JobStatus:
    """Trigger hook mining. Returns job id."""
    # Stub
    raise HTTPException(status_code=501, detail="Not implemented")


@router.post("/campaigns/{campaign_id}/write", status_code=202)
async def trigger_copy_writing(
    campaign_id: UUID,
    session: AsyncSession = Depends(),
    auth: None = Depends(verify_auth),
) -> JobStatus:
    """Trigger copy generation. Returns job id."""
    # Stub
    raise HTTPException(status_code=501, detail="Not implemented")


@router.post("/campaigns/{campaign_id}/dry-run", status_code=202)
async def trigger_dry_run(
    campaign_id: UUID,
    session: AsyncSession = Depends(),
    auth: None = Depends(verify_auth),
) -> JobStatus:
    """Dry-run: render without SMTP. Returns job id."""
    # Stub
    raise HTTPException(status_code=501, detail="Not implemented")


@router.post("/campaigns/{campaign_id}/launch", status_code=202)
async def launch_campaign(
    campaign_id: UUID,
    session: AsyncSession = Depends(),
    auth: None = Depends(verify_auth),
) -> JobStatus:
    """Launch campaign (explicit). Returns job id.

    Blocked if linter checks fail (422 with details).
    """
    # Stub
    raise HTTPException(status_code=501, detail="Not implemented")


@router.post("/campaigns/{campaign_id}/pause", status_code=204)
async def pause_campaign(
    campaign_id: UUID,
    session: AsyncSession = Depends(),
    auth: None = Depends(verify_auth),
) -> None:
    """Pause campaign immediately."""
    # Stub
    raise HTTPException(status_code=501, detail="Not implemented")


# ============================================================================
# Jobs
# ============================================================================


@router.get("/jobs/{job_id}")
async def get_job_status(
    job_id: UUID,
    session: AsyncSession = Depends(),
    auth: None = Depends(verify_auth),
) -> JobStatus:
    """Get job status, progress, and errors."""
    # Stub
    raise HTTPException(status_code=501, detail="Not implemented")


# ============================================================================
# Leads
# ============================================================================


@router.get("/campaigns/{campaign_id}/leads")
async def list_leads(
    campaign_id: UUID,
    verified: str | None = None,
    has_person: bool | None = None,
    has_hook: bool | None = None,
    page: int = 1,
    page_size: int = 50,
    session: AsyncSession = Depends(),
    auth: None = Depends(verify_auth),
) -> LeadsListResponse:
    """List leads for a campaign with filtering and pagination."""
    # Stub
    raise HTTPException(status_code=501, detail="Not implemented")


@router.get("/leads/{lead_id}")
async def get_lead(
    lead_id: UUID,
    session: AsyncSession = Depends(),
    auth: None = Depends(verify_auth),
) -> LeadResponse:
    """Get lead detail with facts and provenance (source URLs)."""
    # Stub
    raise HTTPException(status_code=501, detail="Not implemented")


# ============================================================================
# Messages
# ============================================================================


@router.get("/campaigns/{campaign_id}/messages")
async def list_messages(
    campaign_id: UUID,
    state: str | None = None,
    page: int = 1,
    page_size: int = 50,
    session: AsyncSession = Depends(),
    auth: None = Depends(verify_auth),
) -> LeadsListResponse:
    """List messages for review."""
    # Stub
    raise HTTPException(status_code=501, detail="Not implemented")


@router.patch("/messages/{message_id}")
async def edit_message(
    message_id: UUID,
    body: str,
    subject: str,
    session: AsyncSession = Depends(),
    auth: None = Depends(verify_auth),
) -> MessageResponse:
    """Edit a message before sending."""
    # Stub
    raise HTTPException(status_code=501, detail="Not implemented")


@router.post("/messages/{message_id}/approve", status_code=204)
async def approve_message(
    message_id: UUID,
    session: AsyncSession = Depends(),
    auth: None = Depends(verify_auth),
) -> None:
    """Approve a message for sending."""
    # Stub
    raise HTTPException(status_code=501, detail="Not implemented")


# ============================================================================
# Mailboxes
# ============================================================================


@router.get("/mailboxes")
async def list_mailboxes(
    session: AsyncSession = Depends(),
    auth: None = Depends(verify_auth),
) -> list[MailboxResponse]:
    """List mailboxes with health scores and DNS status."""
    # Stub
    raise HTTPException(status_code=501, detail="Not implemented")


@router.post("/mailboxes", status_code=201)
async def add_mailbox(
    address: str,
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_password: str,
    session: AsyncSession = Depends(),
    auth: None = Depends(verify_auth),
) -> MailboxResponse:
    """Add mailbox. Triggers DNS preflight automatically.

    Note: Credentials are encrypted at rest and never returned.
    """
    # Stub
    raise HTTPException(status_code=501, detail="Not implemented")


@router.post("/mailboxes/{mailbox_id}/recheck-dns", status_code=204)
async def recheck_dns(
    mailbox_id: UUID,
    session: AsyncSession = Depends(),
    auth: None = Depends(verify_auth),
) -> None:
    """Re-run DNS preflight checks."""
    # Stub
    raise HTTPException(status_code=501, detail="Not implemented")


# ============================================================================
# Analytics
# ============================================================================


@router.get("/analytics/campaigns/{campaign_id}")
async def get_campaign_analytics(
    campaign_id: UUID,
    session: AsyncSession = Depends(),
    auth: None = Depends(verify_auth),
) -> AnalyticsReport:
    """Get campaign funnel, yield, and reply analytics (Session 19)."""
    # Stub
    raise HTTPException(status_code=501, detail="Not implemented")


@router.get("/analytics/sources")
async def get_source_yield(
    campaign_id: UUID | None = None,
    session: AsyncSession = Depends(),
    auth: None = Depends(verify_auth),
) -> list[dict[str, str | float]]:
    """Get per-source yield with deltas vs baseline."""
    # Stub
    raise HTTPException(status_code=501, detail="Not implemented")


# ============================================================================
# Alerts
# ============================================================================


@router.get("/alerts")
async def list_alerts(
    session: AsyncSession = Depends(),
    auth: None = Depends(verify_auth),
) -> list[AlertResponse]:
    """List unacknowledged alerts."""
    # Stub
    raise HTTPException(status_code=501, detail="Not implemented")


@router.post("/alerts/{alert_id}/ack", status_code=204)
async def acknowledge_alert(
    alert_id: UUID,
    session: AsyncSession = Depends(),
    auth: None = Depends(verify_auth),
) -> None:
    """Mark alert as acknowledged."""
    # Stub
    raise HTTPException(status_code=501, detail="Not implemented")


# ============================================================================
# Suppressions
# ============================================================================


@router.get("/suppressions")
async def list_suppressions(
    session: AsyncSession = Depends(),
    auth: None = Depends(verify_auth),
) -> list[dict[str, str]]:
    """List active suppressions."""
    # Stub
    raise HTTPException(status_code=501, detail="Not implemented")


@router.post("/suppressions", status_code=201)
async def add_suppression(
    req: SuppressionAdd,
    session: AsyncSession = Depends(),
    auth: None = Depends(verify_auth),
) -> dict[str, str]:
    """Add an address or domain to suppression list."""
    # Stub
    raise HTTPException(status_code=501, detail="Not implemented")


# ============================================================================
# Public (no auth required)
# ============================================================================


@router.post("/u/{token}", status_code=204)
async def one_click_unsubscribe(
    token: str,
    session: AsyncSession = Depends(),
) -> None:
    """One-click unsubscribe via RFC 8058 token. No auth required.

    Token is HMAC-signed and scoped to one send. Rejects tampered tokens.
    """
    # Stub
    raise HTTPException(status_code=501, detail="Not implemented")


__all__ = ["router", "verify_auth"]
