"""Mailbox model — stub. Behaviour lands in Session 14 (mailbox pool & DNS
preflight).

provider deliberately can only be one of MailboxProvider's three values —
Google Workspace, Microsoft 365, or generic SMTP. SendGrid/Mailgun/Postmark/
SES cannot be represented in this schema at all (CLAUDE.md §10).

encrypted_credentials is opaque ciphertext from Session 14's CredentialVault
— this model never sees or logs plaintext credentials.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Enum as SAEnum
from sqlalchemy import Integer, LargeBinary, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import BaseModel
from app.db.models.enums import MailboxProvider


class Mailbox(BaseModel):
    __tablename__ = "mailboxes"

    provider: Mapped[MailboxProvider] = mapped_column(
        SAEnum(MailboxProvider, name="mailbox_provider"), nullable=False
    )
    email_address: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    encrypted_credentials: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    health_score: Mapped[float] = mapped_column(nullable=False, default=1.0)
    warmup_stage: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    daily_cap: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    # SPF/DKIM/DMARC/PTR/MX results with fix text, from the Session 14
    # preflight — this is what Session 21's mailbox card renders directly.
    dns_status: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
