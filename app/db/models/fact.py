"""The Fact model — append-only provenance for every enriched value.

CLAUDE.md rule 2.2: no value enters the database as a bare string. Every
scraped or resolved fact — a person's name, an email address, a hook — is a
row here, carrying its confidence, its source resolver, the URL it came
from, and when it was retrieved. An LLM writing copy reads from these rows,
never from a raw scraped string on another table.

Corrections never UPDATE a row. They INSERT a new fact and set the old
row's superseded_by to the new row's id, both in one transaction (see
app/db/repositories/fact_repository.py). This keeps the full history of
what the system believed and when — which matters the day a fact turns out
to be wrong and someone needs to know how it got into a sent email.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Float, ForeignKey, Index, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import BaseModel
from app.db.models.enums import SubjectType


class Fact(BaseModel):
    __tablename__ = "facts"

    subject_type: Mapped[SubjectType] = mapped_column(
        SAEnum(SubjectType, name="subject_type"), nullable=False
    )
    # Not a real FK: subject_id can point at companies.id, people.id, or
    # email_addresses.id depending on subject_type, and Postgres has no
    # polymorphic FK. Repository-layer lookups are responsible for validity.
    subject_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    field: Mapped[str] = mapped_column(String, nullable=False)  # "person_name", "email", ...
    value: Mapped[str] = mapped_column(String, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)  # resolver name
    source_url: Mapped[str | None] = mapped_column(String, nullable=True)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Facts sharing an independence_key come from correlated sources (e.g.
    # two scrapers reading the same page) and must not be treated as
    # independent confirmation when merging confidence (Session 03).
    independence_key: Mapped[str | None] = mapped_column(String, nullable=True)
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("facts.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        Index("ix_facts_subject", "subject_type", "subject_id", "field"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_facts_conf"),
    )
