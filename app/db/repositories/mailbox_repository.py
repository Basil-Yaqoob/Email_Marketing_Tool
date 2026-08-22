"""MailboxRepository — connected sending mailboxes and their credentials.

Credentials never cross this boundary in plaintext in either direction
except through the two explicitly-named methods. `add`/`list`/`get` deal
in `MailboxView`, which has no credential field at all, so a route handler
or template physically cannot render one. Only `credentials_for` returns
plaintext, and only the SMTP/IMAP connection code calls it, at connection
time (CLAUDE.md §2.4 and Session 14's rule).

A mailbox row bundles SMTP and IMAP settings into one encrypted blob
rather than a column per field: the vault encrypts one string, and
splitting hostnames into cleartext columns beside an encrypted password
leaks half the connection details for no benefit.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.enums import MailboxProvider
from app.db.models.mailbox import Mailbox
from app.sending.credentials import CredentialVault


class MailboxCredentials(BaseModel):
    """Everything needed to connect. Encrypted as one JSON blob.

    `SecretStr` so an accidental repr/log of this object during the
    connection flow prints '**********' rather than the password.
    """

    model_config = ConfigDict(frozen=True)

    smtp_host: str
    smtp_port: int = 587
    smtp_username: str
    smtp_password: SecretStr
    imap_host: str | None = None
    imap_port: int = 993
    imap_username: str | None = None
    imap_password: SecretStr | None = None

    def as_plaintext_json(self) -> str:
        payload = self.model_dump()
        payload["smtp_password"] = self.smtp_password.get_secret_value()
        if self.imap_password is not None:
            payload["imap_password"] = self.imap_password.get_secret_value()
        return json.dumps(payload)


class MailboxCreate(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: MailboxProvider
    email_address: str = Field(min_length=3)
    credentials: MailboxCredentials
    daily_cap: int = 5
    warmup_stage: int = 0


class MailboxView(BaseModel):
    """The safe projection. Deliberately has no credentials field, so no
    caller can leak one by forgetting to strip it.
    """

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    provider: MailboxProvider
    email_address: str
    health_score: float
    warmup_stage: int
    daily_cap: int
    dns_status: dict[str, Any]

    @classmethod
    def of(cls, row: Mailbox) -> MailboxView:
        return cls(
            id=row.id,
            provider=row.provider,
            email_address=row.email_address,
            health_score=row.health_score,
            warmup_stage=row.warmup_stage,
            daily_cap=row.daily_cap,
            dns_status=row.dns_status,
        )


class MailboxRepository:
    def __init__(self, session: AsyncSession, vault: CredentialVault) -> None:
        self._session = session
        self._vault = vault

    async def add(self, mailbox: MailboxCreate) -> MailboxView:
        row = Mailbox(
            provider=mailbox.provider,
            email_address=mailbox.email_address,
            encrypted_credentials=self._vault.encrypt(mailbox.credentials.as_plaintext_json()),
            daily_cap=mailbox.daily_cap,
            warmup_stage=mailbox.warmup_stage,
        )
        self._session.add(row)
        await self._session.flush()
        return MailboxView.of(row)

    async def list(self) -> list[MailboxView]:
        stmt = select(Mailbox).order_by(Mailbox.created_at, Mailbox.id)
        result = await self._session.execute(stmt)
        return [MailboxView.of(row) for row in result.scalars().all()]

    async def get(self, mailbox_id: uuid.UUID) -> MailboxView | None:
        row = await self._session.get(Mailbox, mailbox_id)
        return MailboxView.of(row) if row is not None else None

    async def credentials_for(self, mailbox_id: uuid.UUID) -> MailboxCredentials:
        """Decrypt. The only method that returns plaintext -- call it at
        connection time and let the result go out of scope immediately.
        """
        row = await self._session.get(Mailbox, mailbox_id)
        if row is None:
            raise ValueError(f"Mailbox {mailbox_id} does not exist")
        return MailboxCredentials.model_validate(
            json.loads(self._vault.decrypt(row.encrypted_credentials))
        )

    async def set_health(self, mailbox_id: uuid.UUID, score: float) -> MailboxView:
        row = await self._require(mailbox_id)
        row.health_score = score
        await self._session.flush()
        return MailboxView.of(row)

    async def set_warmup_stage(self, mailbox_id: uuid.UUID, stage: int, cap: int) -> MailboxView:
        """Advance warmup. Stage and cap move together -- a stage without
        its matching cap would let a barely-warm mailbox send at full rate.
        """
        row = await self._require(mailbox_id)
        row.warmup_stage = stage
        row.daily_cap = cap
        await self._session.flush()
        return MailboxView.of(row)

    async def set_dns_status(self, mailbox_id: uuid.UUID, status: dict[str, Any]) -> MailboxView:
        row = await self._require(mailbox_id)
        row.dns_status = status
        await self._session.flush()
        return MailboxView.of(row)

    async def delete(self, mailbox_id: uuid.UUID) -> None:
        row = await self._require(mailbox_id)
        await self._session.delete(row)
        await self._session.flush()

    async def _require(self, mailbox_id: uuid.UUID) -> Mailbox:
        row = await self._session.get(Mailbox, mailbox_id)
        if row is None:
            raise ValueError(f"Mailbox {mailbox_id} does not exist")
        return row


__all__ = [
    "MailboxCreate",
    "MailboxCredentials",
    "MailboxRepository",
    "MailboxView",
]
