"""REST API surface."""

from app.api.errors import (
    APIError,
    ErrorDetail,
    ErrorEnvelope,
    exception_to_status,
    make_error_envelope,
)
from app.api.models import (
    AlertResponse,
    AnalyticsReport,
    CampaignCreate,
    CampaignResponse,
    CampaignUpdate,
    FactRecord,
    JobError,
    JobState,
    JobStatus,
    LeadResponse,
    LeadsListResponse,
    MailboxResponse,
    MessageResponse,
    SuppressionAdd,
)
from app.api.routes import router, verify_auth

__all__ = [
    "APIError",
    "AlertResponse",
    "AnalyticsReport",
    "CampaignCreate",
    "CampaignResponse",
    "CampaignUpdate",
    "ErrorDetail",
    "ErrorEnvelope",
    "FactRecord",
    "JobError",
    "JobState",
    "JobStatus",
    "LeadResponse",
    "LeadsListResponse",
    "MailboxResponse",
    "MessageResponse",
    "SuppressionAdd",
    "exception_to_status",
    "make_error_envelope",
    "router",
    "verify_auth",
]
