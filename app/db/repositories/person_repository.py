"""PersonRepository — the people enrichment finds at each company.

`best_for_company` encodes the decision the copywriter actually needs: of
several contacts at one business, which one gets written to. Role class
ranks ahead of anything else -- an OWNER beats a MARKETING contact even if
the marketing contact was found with higher confidence, because the
product is cold outreach to decision makers (CLAUDE.md §10's 55-70%
decision-maker coverage is the number this feeds).
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.expression import Case

from app.db.models.enums import RoleClass
from app.db.models.person import Person

# Lower sorts first. OTHER last so an unclassified contact never outranks
# a known decision maker.
_ROLE_RANK = {
    RoleClass.OWNER: 0,
    RoleClass.MANAGER: 1,
    RoleClass.MARKETING: 2,
    RoleClass.OTHER: 3,
}


class PersonCreate(BaseModel):
    model_config = ConfigDict(frozen=True)

    company_id: uuid.UUID
    full_name: str = Field(min_length=1)
    title: str | None = None
    role_class: RoleClass = RoleClass.OTHER
    seniority: str | None = None


class PersonRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, person: PersonCreate) -> Person:
        row = Person(**person.model_dump())
        self._session.add(row)
        await self._session.flush()
        return row

    async def bulk_add(self, people: list[PersonCreate]) -> list[Person]:
        if not people:
            return []
        rows = [Person(**p.model_dump()) for p in people]
        self._session.add_all(rows)
        await self._session.flush()
        return rows

    async def get(self, person_id: uuid.UUID) -> Person | None:
        return await self._session.get(Person, person_id)

    async def for_company(self, company_id: uuid.UUID) -> list[Person]:
        """Everyone known at a company, decision makers first."""
        stmt = (
            select(Person)
            .where(Person.company_id == company_id)
            .order_by(self._role_ordering(), Person.id)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def best_for_company(self, company_id: uuid.UUID) -> Person | None:
        """The single contact to write to, or None if the company has no
        person yet -- a normal outcome, not a failure (coverage tops out
        well short of 100%).
        """
        stmt = (
            select(Person)
            .where(Person.company_id == company_id)
            .order_by(self._role_ordering(), Person.id)
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalars().first()

    @staticmethod
    def _role_ordering() -> Case[int]:
        """Rank by seniority of role rather than by enum declaration order,
        which is alphabetical and meaningless here.

        Built from explicit column comparisons rather than the dict form of
        `case()`. The dict form renders its keys as literals, which emits
        the StrEnum's *value* ('owner') while the Postgres enum column
        stores member *names* ('OWNER') -- so it fails at query time rather
        than at import. Comparing against the column lets SQLAlchemy bind
        each member through that column's own type.
        """
        return case(
            *((Person.role_class == role, rank) for role, rank in _ROLE_RANK.items()),
            else_=len(_ROLE_RANK),
        )


__all__ = ["PersonCreate", "PersonRepository"]
