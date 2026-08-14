"""Shared enums for the core schema.

CLAUDE.md gotcha (Session 02): these map to native Postgres enum types.
Adding a value later needs an explicit `ALTER TYPE` migration, so the value
sets here are meant to be complete for what later sessions actually build,
not guessed at loosely.
"""

from __future__ import annotations

import enum


class CampaignStatus(enum.StrEnum):
    DRAFT = "draft"
    DISCOVERING = "discovering"
    ENRICHING = "enriching"
    RESEARCHING = "researching"
    WRITING = "writing"
    READY = "ready"
    SENDING = "sending"
    PAUSED = "paused"
    COMPLETED = "completed"


class SubjectType(enum.StrEnum):
    """What a Fact is about — see app/db/models/fact.py."""

    COMPANY = "company"
    PERSON = "person"
    EMAIL = "email"


class RoleClass(enum.StrEnum):
    """Coarse classification of a person's role, used to route messaging
    angles and to filter out role accounts (info@, sales@) from outreach.
    """

    OWNER = "owner"
    MANAGER = "manager"
    MARKETING = "marketing"
    OTHER = "other"


class VerifyStatus(enum.StrEnum):
    """Email verification outcome.

    UNKNOWN is a first-class outcome, not an error. Google Workspace and
    Microsoft 365 hosted domains land here permanently because their MTAs
    accept-then-bounce rather than rejecting at RCPT TO. Sending policy
    (Session 15) routes UNKNOWN into a lower-volume track — it never gets
    treated as a failed verification.
    """

    UNCHECKED = "unchecked"
    VALID = "valid"
    INVALID = "invalid"
    CATCH_ALL = "catch_all"
    UNKNOWN = "unknown"
    ROLE = "role"


class ResolverOutcome(enum.StrEnum):
    """One row per resolver attempt in resolver_runs — including misses.

    This is the anti-silent-failure table: a MISS or ERROR is recorded with
    the same weight as a HIT, so a yield collapse to zero is visible in the
    telemetry rather than absent from it.
    """

    HIT = "hit"
    MISS = "miss"
    ERROR = "error"
    SKIPPED = "skipped"


class MessageStatus(enum.StrEnum):
    """Review-queue state for a drafted message (Session 13, reviewed in 21)."""

    DRAFT = "draft"
    APPROVED = "approved"
    REJECTED = "rejected"
    SENT = "sent"


class MailboxProvider(enum.StrEnum):
    """Deliberately excludes SendGrid/Mailgun/Postmark/SES — all four ban
    cold outreach in their AUPs (CLAUDE.md §10). This enum is the schema-level
    enforcement of that rule: those providers cannot be represented at all.
    """

    GOOGLE_WORKSPACE = "google_workspace"
    MICROSOFT_365 = "microsoft_365"
    GENERIC_SMTP = "generic_smtp"


class SendStatus(enum.StrEnum):
    QUEUED = "queued"
    SENT = "sent"
    BOUNCED = "bounced"
    FAILED = "failed"


class ReplyClassification(enum.StrEnum):
    """Session 18's classification buckets, each with its own automatic action."""

    INTERESTED = "interested"
    NOT_INTERESTED = "not_interested"
    OUT_OF_OFFICE = "out_of_office"
    WRONG_PERSON = "wrong_person"
    UNSUBSCRIBE = "unsubscribe"
    AUTO_REPLY = "auto_reply"
    BOUNCE = "bounce"


class SeedPlacement(enum.StrEnum):
    """Where a seed-list test message actually landed (Session 19)."""

    INBOX = "inbox"
    PROMOTIONS = "promotions"
    SPAM = "spam"
    NOT_FOUND = "not_found"
    UNKNOWN = "unknown"
