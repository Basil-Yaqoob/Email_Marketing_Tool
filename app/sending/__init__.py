"""Sending infrastructure: mailbox pool, DNS preflight, health scoring."""

from app.sending.credentials import CredentialVault
from app.sending.dns_preflight import (
    CheckStatus,
    DNSCheck,
    DNSPreflight,
    check_alignment,
    check_dkim,
    check_dmarc,
    check_mx,
    check_ptr,
    check_spf,
    preflight,
)
from app.sending.health import health_score
from app.sending.warmup import (
    PROVIDER_CAPS,
    WARMUP_STAGES,
    daily_cap_for_stage,
    next_warmup_stage,
    warmup_advice,
)

__all__ = [
    "PROVIDER_CAPS",
    "WARMUP_STAGES",
    "CheckStatus",
    "CredentialVault",
    "DNSCheck",
    "DNSPreflight",
    "check_alignment",
    "check_dkim",
    "check_dmarc",
    "check_mx",
    "check_ptr",
    "check_spf",
    "daily_cap_for_stage",
    "health_score",
    "next_warmup_stage",
    "preflight",
    "warmup_advice",
]
