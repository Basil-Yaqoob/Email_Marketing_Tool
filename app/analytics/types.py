"""Analytics types for yield reporting, anomalies, and health tracking."""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime


class AnomalyKind(enum.StrEnum):
    """Types of anomalies that warrant alerts."""

    YIELD_COLLAPSE = "yield_collapse"  # Hit rate fell >50% vs baseline
    ZERO_YIELD = "zero_yield"  # >=20 attempts, 0 hits
    ERROR_SPIKE = "error_spike"  # Error rate >5%
    BOUNCE_CLIMB = "bounce_climb"  # Trending toward 3%
    COMPLAINT_SPIKE = "complaint_spike"  # Complaint rate spike
    COST_OVERRUN = "cost_overrun"  # >150% of estimate
    STALE_STAGE = "stale_stage"  # Produced nothing in N hours


class Placement(enum.StrEnum):
    """Email folder placement from seed testing."""

    INBOX = "inbox"
    PROMOTIONS = "promotions"
    SPAM = "spam"
    MISSING = "missing"


@dataclass(frozen=True)
class SourceYield:
    """Yield stats for one resolver (from resolver_runs telemetry)."""

    resolver_name: str
    attempts: int
    hits: int
    hit_rate: float  # 0.0 to 1.0
    avg_confidence: float
    avg_latency_ms: float
    total_cost_usd: float
    baseline_hit_rate: float | None  # Previous window
    delta_vs_baseline: float | None  # Percentage point change


@dataclass(frozen=True)
class Anomaly:
    """A detected anomaly requiring operator attention."""

    id: str
    kind: AnomalyKind
    campaign_id: str | None
    resolver_name: str | None
    message: str
    severity: int  # 1-5
    detected_at: datetime
    acknowledged: bool
    acknowledged_at: datetime | None


@dataclass(frozen=True)
class SeedPlacement:
    """Result from one seed account placement test."""

    seed_email: str
    provider: str  # gmail, outlook, yahoo, corporate
    placement: Placement
    checked_at: datetime


@dataclass(frozen=True)
class HealthSnapshot:
    """Health score history for one mailbox."""

    mailbox_id: str
    score: float  # 0.0 to 1.0
    bounce_rate: float
    complaint_rate: float
    reply_rate: float
    warmup_stage: int
    measured_at: datetime


@dataclass(frozen=True)
class CampaignFunnel:
    """Campaign funnel matching doc/00-OVERVIEW.md."""

    campaign_id: str
    discovered: int
    with_website: int
    person_found: int
    email_found: int
    verified: int
    sent: int
    replied: int


__all__ = [
    "Anomaly",
    "AnomalyKind",
    "CampaignFunnel",
    "HealthSnapshot",
    "Placement",
    "SeedPlacement",
    "SourceYield",
]
