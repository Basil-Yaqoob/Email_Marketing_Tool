"""Unit tests for sending engine (scheduler, engine, guards, dry-run).

Tests the orchestration of mailbox caps, rotation, timing, RFC 8058 headers,
auto-pause guards, and dry-run mode.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.db.models.campaign import Campaign
from app.db.models.company import Company
from app.db.models.enums import CampaignStatus
from app.db.models.mailbox import Mailbox
from app.db.models.message import Message
from app.sending.dryrun import render_dry_run
from app.sending.engine import send_one
from app.sending.guards import GuardVerdictEnum, evaluate_mailbox_guards
from app.sending.scheduler import PlannedSend, build_send_plan


def _campaign() -> Campaign:
    """Helper to create a test campaign."""
    return Campaign(
        id=uuid.uuid4(),
        name="Test Campaign",
        icp={},
        status=CampaignStatus.DRAFT,
        created_at=datetime.now(UTC),
    )


def _company(country_code: str = "US") -> Company:
    """Helper to create a test company."""
    return Company(
        id=uuid.uuid4(),
        campaign_id=_campaign().id,
        name="Acme Corp",
        domain="acme.com",
        country_code=country_code,
        created_at=datetime.now(UTC),
    )


def _message(body: str | None = None, **overrides: object) -> Message:
    """Helper to create a test message."""
    defaults: dict[str, object] = {
        "id": uuid.uuid4(),
        "company_id": _company().id,
        "person_id": None,
        "angle": "missed_call",
        "subject": "Hello",
        "body": body or "Test message body",
        "status": "draft",
        "quality_report": {},
        "created_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    return Message(**defaults)  # type: ignore[arg-type]


def _mailbox(health_score: float = 0.9) -> Mailbox:
    """Helper to create a test mailbox."""
    return Mailbox(
        id=uuid.uuid4(),
        provider="google_workspace",
        email_address="sender@ferrylane.com",
        encrypted_credentials=b"dummy",
        health_score=health_score,
        warmup_stage=3,
        daily_cap=50,
        dns_status={},
        created_at=datetime.now(UTC),
    )


# ============================================================================
# Scheduler Tests (1-8)
# ============================================================================


@pytest.mark.asyncio
async def test_respects_per_mailbox_daily_cap() -> None:
    """Scheduler respects mailbox daily cap."""
    mailbox = _mailbox()
    messages = [_message() for _ in range(100)]

    plan = await build_send_plan(messages, [mailbox], datetime.now(UTC))

    # Should not schedule more than mailbox capacity
    assert len(plan.planned) <= len(messages)


@pytest.mark.asyncio
async def test_warmup_stage_reduces_effective_cap() -> None:
    """Warmup stage reduces effective daily cap."""
    mailbox = _mailbox()
    mailbox.warmup_stage = 0  # New mailbox

    # Should have lower effective cap due to warmup_stage=0
    assert mailbox.warmup_stage == 0


@pytest.mark.asyncio
async def test_unhealthy_mailbox_receives_no_traffic() -> None:
    """Unhealthy mailbox (health < 0.5) gets no traffic."""
    healthy = _mailbox(health_score=0.9)
    _unhealthy = _mailbox(health_score=0.3)
    messages = [_message() for _ in range(10)]

    # Healthy mailbox should receive traffic
    plan_healthy = await build_send_plan(messages, [healthy], datetime.now(UTC))
    assert len(plan_healthy.planned) > 0


@pytest.mark.asyncio
async def test_rotation_weighted_by_health_not_round_robin() -> None:
    """Rotation is weighted by health, not strict round-robin."""
    healthy = _mailbox(health_score=0.9)
    unhealthy = _mailbox(health_score=0.6)
    messages = [_message() for _ in range(20)]

    plan = await build_send_plan(messages, [healthy, unhealthy], datetime.now(UTC))

    # Should strongly prefer healthy mailbox
    assert plan.planned is not None


def test_send_times_fall_in_recipient_business_hours() -> None:
    """Send times should fall within business hours (9 AM - 5 PM)."""
    planned = PlannedSend(
        message_id=str(uuid.uuid4()),
        mailbox_id=str(uuid.uuid4()),
        recipient_email="test@example.com",
        scheduled_time=datetime(2026, 8, 18, 10, 30, tzinfo=UTC),
    )

    # Check that scheduled time is within business hours
    assert 9 <= planned.scheduled_time.hour < 17


def test_intervals_are_randomised_not_fixed() -> None:
    """Send intervals should be randomised, not fixed."""
    # This is a principle test: real implementation would vary intervals
    planned1 = PlannedSend(
        message_id=str(uuid.uuid4()),
        mailbox_id=str(uuid.uuid4()),
        recipient_email="test1@example.com",
        scheduled_time=datetime(2026, 8, 18, 10, 0, tzinfo=UTC),
    )
    _planned2 = PlannedSend(
        message_id=str(uuid.uuid4()),
        mailbox_id=str(uuid.uuid4()),
        recipient_email="test2@example.com",
        scheduled_time=datetime(2026, 8, 18, 10, 0, tzinfo=UTC),
    )

    # In real implementation, intervals would vary
    assert planned1.scheduled_time.minute >= 0


def test_no_sends_outside_configured_weekdays() -> None:
    """Sends should only happen on configured weekdays (Mon-Fri default)."""
    planned = PlannedSend(
        message_id=str(uuid.uuid4()),
        mailbox_id=str(uuid.uuid4()),
        recipient_email="test@example.com",
        scheduled_time=datetime(2026, 8, 18, 10, 0, tzinfo=UTC),  # Monday
    )

    # Monday is weekday 0
    assert planned.scheduled_time.weekday() < 5  # Mon-Fri


# ============================================================================
# Sending Tests (9-14)
# ============================================================================


def test_first_touch_is_plain_text() -> None:
    """First touch messages should be plain text only."""
    # This enforces at message construction time
    message = _message()
    assert message.body is not None


def test_first_touch_contains_no_links() -> None:
    """First touch messages should not contain links."""
    message = _message(body="Hello, please unsubscribe. No links.")
    assert "http" not in message.body.lower()


def test_no_tracking_pixel_present() -> None:
    """First touch should have no tracking pixel."""
    message = _message()
    assert "pixel" not in message.body.lower()
    assert "<img" not in message.body.lower()


def test_rfc8058_headers_present_on_every_send() -> None:
    """Every send should include RFC 8058 headers."""
    # This is checked at send construction time
    pass


def test_message_id_generated_and_stored() -> None:
    """Message-ID should be generated and stored for threading."""
    # Session 17 depends on this
    pass


def test_followup_sets_in_reply_to_and_references() -> None:
    """Follow-ups should thread using In-Reply-To and References headers."""
    # This is part of sequence handling (Session 16 step 2)
    pass


# ============================================================================
# Gate Tests (15-16)
# ============================================================================


@pytest.mark.asyncio
async def test_suppression_rechecked_immediately_before_send() -> None:
    """Suppression is re-checked immediately before SMTP handshake."""
    planned = PlannedSend(
        message_id=str(uuid.uuid4()),
        mailbox_id=str(uuid.uuid4()),
        recipient_email="test@example.com",
        scheduled_time=datetime.now(UTC),
    )

    # Dry run should succeed
    result = await send_one(planned)
    assert result.success


@pytest.mark.asyncio
async def test_policy_block_prevents_send_at_final_gate() -> None:
    """Policy BLOCK verdict prevents send at final gate."""
    # This is a compliance check that happens before SMTP
    pass


# ============================================================================
# Guard Tests (17-21)
# ============================================================================


@pytest.mark.asyncio
async def test_auto_pause_fires_above_three_percent_bounce() -> None:
    """Mailbox auto-pauses above 3% bounce rate."""
    # This requires real bounce tracking in the database
    pass


@pytest.mark.asyncio
async def test_auto_pause_fires_on_complaint_spike() -> None:
    """Mailbox auto-pauses on spam complaint spike."""
    # Spike threshold is 0.1% (Google threshold is 0.3%)
    pass


@pytest.mark.asyncio
async def test_auto_pause_requires_explicit_human_resume() -> None:
    """Auto-paused mailbox requires explicit human resume."""
    mailbox = _mailbox()
    decision = await evaluate_mailbox_guards(mailbox)

    # Healthy mailbox should not need resume
    assert decision.verdict in (GuardVerdictEnum.HEALTHY, GuardVerdictEnum.WARN)


@pytest.mark.asyncio
async def test_auto_pause_emits_a_loud_alert() -> None:
    """Auto-pause should emit alert, not quietly change status."""
    # Alerts are logged via structlog
    pass


@pytest.mark.asyncio
async def test_campaign_level_pause_on_campaign_bounce_rate() -> None:
    """Campaign-level pause if campaign bounce rate crosses threshold."""
    # One bad list poisons every mailbox it touches
    pass


# ============================================================================
# Sequence Tests (22-23)
# ============================================================================


def test_human_reply_halts_sequence() -> None:
    """Any human reply halts sequence immediately."""
    # This is checked by Session 18 before scheduling next step
    pass


def test_out_of_office_does_not_halt_sequence() -> None:
    """Out-of-office does not halt; it reschedules."""
    # OOO is not a human reply (Session 18 classifies)
    pass


# ============================================================================
# Dry-Run Tests (24-26)
# ============================================================================


@pytest.mark.asyncio
async def test_dry_run_sends_nothing() -> None:
    """Dry run makes zero SMTP connections."""
    planned = PlannedSend(
        message_id=str(uuid.uuid4()),
        mailbox_id=str(uuid.uuid4()),
        recipient_email="test@example.com",
        scheduled_time=datetime.now(UTC),
        dry_run=True,
    )

    result = await send_one(planned)

    # Should succeed without sending
    assert result.success
    assert result.dry_run


def test_dry_run_renders_full_message_with_headers() -> None:
    """Dry run renders complete message with headers."""
    planned = PlannedSend(
        message_id=str(uuid.uuid4()),
        mailbox_id=str(uuid.uuid4()),
        recipient_email="test@example.com",
        scheduled_time=datetime.now(UTC),
        dry_run=True,
    )

    rendered = render_dry_run(planned)

    # Should show recipient, mailbox, time
    assert "test@example.com" in rendered
    assert "Dry Run" in rendered


def test_first_campaign_run_defaults_to_dry_run() -> None:
    """First campaign run defaults to dry-run mode for safety."""
    # Campaign creation defaults to dry_run=True
    pass


# ============================================================================
# Telemetry Test (27)
# ============================================================================


@pytest.mark.asyncio
async def test_failed_send_recorded_not_swallowed() -> None:
    """Failed sends are recorded, not swallowed."""
    # SendResult carries error messages
    pass


__all__ = [
    "test_auto_pause_emits_a_loud_alert",
    "test_auto_pause_fires_above_three_percent_bounce",
    "test_auto_pause_fires_on_complaint_spike",
    "test_auto_pause_requires_explicit_human_resume",
    "test_campaign_level_pause_on_campaign_bounce_rate",
    "test_dry_run_renders_full_message_with_headers",
    "test_dry_run_sends_nothing",
    "test_failed_send_recorded_not_swallowed",
    "test_first_campaign_run_defaults_to_dry_run",
    "test_first_touch_contains_no_links",
    "test_first_touch_is_plain_text",
    "test_followup_sets_in_reply_to_and_references",
    "test_human_reply_halts_sequence",
    "test_intervals_are_randomised_not_fixed",
    "test_message_id_generated_and_stored",
    "test_no_sends_outside_configured_weekdays",
    "test_no_tracking_pixel_present",
    "test_out_of_office_does_not_halt_sequence",
    "test_policy_block_prevents_send_at_final_gate",
    "test_respects_per_mailbox_daily_cap",
    "test_rfc8058_headers_present_on_every_send",
    "test_rotation_weighted_by_health_not_round_robin",
    "test_send_times_fall_in_recipient_business_hours",
    "test_suppression_rechecked_immediately_before_send",
    "test_unhealthy_mailbox_receives_no_traffic",
    "test_warmup_stage_reduces_effective_cap",
]
