"""Analytics and instrumentation for yield, health, and anomalies."""

from app.analytics.anomaly import detect_anomalies, detect_zero_yield_alert
from app.analytics.campaign_report import campaign_funnel, conversion_rate, per_angle_conversion
from app.analytics.health_history import (
    detect_health_decline,
    health_history,
    record_health_snapshot,
)
from app.analytics.seeds import (
    SEED_ACCOUNTS,
    check_seed_placement,
    compare_placement_history,
    test_all_seeds,
)
from app.analytics.types import (
    Anomaly,
    AnomalyKind,
    CampaignFunnel,
    HealthSnapshot,
    Placement,
    SeedPlacement,
    SourceYield,
)
from app.analytics.yield_report import baseline_hit_rate, source_yield

__all__ = [
    "SEED_ACCOUNTS",
    "Anomaly",
    "AnomalyKind",
    "CampaignFunnel",
    "HealthSnapshot",
    "Placement",
    "SeedPlacement",
    "SourceYield",
    "baseline_hit_rate",
    "campaign_funnel",
    "check_seed_placement",
    "compare_placement_history",
    "conversion_rate",
    "detect_anomalies",
    "detect_health_decline",
    "detect_zero_yield_alert",
    "health_history",
    "per_angle_conversion",
    "record_health_snapshot",
    "source_yield",
    "test_all_seeds",
]
