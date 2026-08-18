"""Request and response models for the API.

These are the boundary layer — SQLAlchemy models never cross to the client.
"""

from __future__ import annotations

import enum
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class JobState(enum.StrEnum):
    """State of a long-running job."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ABORTED = "aborted"


class JobError(BaseModel):
    """An error that occurred during job execution."""

    code: str
    message: str
    context: dict[str, str] = Field(default_factory=dict)


class JobStatus(BaseModel):
    """Status of a long-running pipeline job."""

    id: UUID
    state: JobState
    progress: float = Field(ge=0.0, le=1.0)  # 0.0-1.0
    processed: int = Field(ge=0)
    total: int = Field(ge=0)
    errors: list[JobError] = Field(default_factory=list)
    aborted_reason: str | None = None  # BatchAbortedError message verbatim
    created_at: datetime
    completed_at: datetime | None = None


class CampaignCreate(BaseModel):
    """Request to create a campaign."""

    name: str
    icp_categories: list[str]
    icp_locations: list[str]
    icp_filters: dict[str, str] = Field(default_factory=dict)
    jurisdiction: str  # e.g., "US", "UK", "DE"


class CampaignUpdate(BaseModel):
    """Request to update a campaign."""

    name: str | None = None
    icp_categories: list[str] | None = None
    icp_locations: list[str] | None = None
    icp_filters: dict[str, str] | None = None


class CampaignResponse(BaseModel):
    """Campaign detail response."""

    id: UUID
    name: str
    icp_categories: list[str]
    icp_locations: list[str]
    icp_filters: dict[str, str]
    jurisdiction: str
    state: str  # DRAFT, RUNNING, PAUSED, COMPLETED
    discovered: int
    with_website: int
    person_found: int
    email_found: int
    verified: int
    sent: int
    replied: int
    created_at: datetime
    updated_at: datetime


class FactRecord(BaseModel):
    """A single fact with its provenance."""

    value: str
    confidence: float = Field(ge=0.0, le=1.0)
    source: str  # Resolver name
    source_url: str | None = None
    retrieved_at: datetime


class LeadResponse(BaseModel):
    """Lead detail with facts and provenance."""

    id: UUID
    campaign_id: UUID
    company_name: str
    website_url: str | None = None
    person_name: str | None = None
    person_title: str | None = None
    email: str | None = None
    email_verified: str | None = None  # "true", "false", "unknown"
    facts: dict[str, list[FactRecord]] = Field(default_factory=dict)  # Grouped by fact type
    created_at: datetime


class LeadsListResponse(BaseModel):
    """Paginated list of leads."""

    leads: list[LeadResponse]
    total: int
    page: int
    page_size: int


class MessageResponse(BaseModel):
    """Message detail for review."""

    id: UUID
    campaign_id: UUID
    lead_id: UUID
    recipient_email: str
    subject: str
    body: str
    quality_report: dict[str, str] = Field(default_factory=dict)  # JSONB from Session 13
    state: str  # DRAFT, SENT, BOUNCED, etc.
    created_at: datetime
    sent_at: datetime | None = None


class MailboxResponse(BaseModel):
    """Mailbox with health and DNS status."""

    id: UUID
    address: str
    health_score: float = Field(ge=0.0, le=1.0)
    bounce_rate: float
    complaint_rate: float
    reply_rate: float
    warmup_stage: int
    dns_status: dict[str, str]  # {"spf": "PASS", "dkim": "FAIL", ...}
    dns_fix_text: dict[str, str] = Field(default_factory=dict)  # Actionable instructions
    created_at: datetime
    # Note: credentials never included


class AnalyticsReport(BaseModel):
    """Campaign analytics report (Session 19)."""

    campaign_id: UUID
    funnel: dict[str, int]  # {discovered, with_website, person_found, ...}
    reply_rate: float
    sources: list[dict[str, str | float]] = Field(default_factory=list)  # Yield per source
    placement: dict[str, dict[str, int]] = Field(
        default_factory=dict
    )  # {provider: {inbox, promotions, ...}}


class AlertResponse(BaseModel):
    """Unacknowledged alert."""

    id: UUID
    kind: str  # ZERO_YIELD, YIELD_COLLAPSE, etc.
    campaign_id: UUID | None
    message: str
    severity: int  # 1-5
    detected_at: datetime


class SuppressionAdd(BaseModel):
    """Request to add a suppression."""

    address: str | None = None
    domain: str | None = None
    scope: str = "ADDRESS"  # ADDRESS, DOMAIN, GLOBAL


class UnsubscribeRequest(BaseModel):
    """Unsubscribe via one-click link (public endpoint)."""

    token: str  # HMAC-signed token from headers


__all__ = [
    "AlertResponse",
    "AnalyticsReport",
    "CampaignCreate",
    "CampaignResponse",
    "CampaignUpdate",
    "FactRecord",
    "JobError",
    "JobState",
    "JobStatus",
    "LeadResponse",
    "LeadsListResponse",
    "MailboxResponse",
    "MessageResponse",
    "SuppressionAdd",
    "UnsubscribeRequest",
]
