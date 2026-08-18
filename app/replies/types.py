"""Reply and bounce types for Session 17 (IMAP ingestion)."""

from __future__ import annotations

import enum
from dataclasses import dataclass


class BounceKind(enum.StrEnum):
    """Types of bounces and bounce-like responses."""

    HARD = "hard"  # 5.x.x status code — permanent failure
    SOFT = "soft"  # 4.x.x status code — transient failure
    COMPLAINT = "complaint"  # Abuse report from recipient
    AUTO_REPLY = "auto_reply"  # Out of office, automated response


class MatchConfidence(enum.StrEnum):
    """How confident we are in a reply-to-send match."""

    EXACT = "exact"  # In-Reply-To, References, or DSN original-message-id
    FUZZY = "fuzzy"  # From + subject similarity within window


@dataclass(frozen=True)
class BounceInfo:
    """Parsed bounce diagnostic information."""

    kind: BounceKind
    status_code: str | None  # e.g. "5.1.1" for hard, "4.4.2" for soft
    reason: str  # Human-readable bounce reason
    address: str  # Email that bounced


@dataclass(frozen=True)
class RawMessage:
    """A raw IMAP message before parsing and matching."""

    uid: int  # IMAP UID, used for high-water mark tracking
    message_id_header: str | None  # RFC 5322 Message-ID
    in_reply_to: str | None  # References the message being replied to
    references: list[str]  # All messages in the thread ancestry
    from_address: str  # Sender's email
    subject: str  # Original subject
    received_at: int  # Unix timestamp
    raw_body: str  # Complete message body for re-parsing
    mailbox_id: str  # UUID of the mailbox this came from


__all__ = ["BounceInfo", "BounceKind", "MatchConfidence", "RawMessage"]
