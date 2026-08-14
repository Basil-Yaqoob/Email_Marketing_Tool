"""The Person model — a named individual associated with a Company."""

from __future__ import annotations

import uuid

from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import BaseModel
from app.db.models.enums import RoleClass


class Person(BaseModel):
    __tablename__ = "people"

    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    full_name: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    role_class: Mapped[RoleClass] = mapped_column(
        SAEnum(RoleClass, name="role_class"), nullable=False, default=RoleClass.OTHER
    )
    # Free-text seniority signal from the source (e.g. "founder", "10+ years")
    # — not modelled as an enum yet since the person resolvers (Session 07,
    # 08) surface it inconsistently across registries and SERP snippets.
    seniority: Mapped[str | None] = mapped_column(String, nullable=True)
