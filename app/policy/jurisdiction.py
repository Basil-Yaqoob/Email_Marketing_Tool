"""Jurisdiction evaluation — load rules from YAML and evaluate leads.

Rules are per-country, loaded from app/policy/rules.yaml. The YAML approach
means the law can change without a code release.

The UK rule is special: it reads entity_type from the facts table. A Ltd/PLC is
ALLOW; a sole_trader or partnership is WARN with the consent requirement.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.company import Company
from app.db.models.enums import SubjectType
from app.db.models.fact import Fact
from app.policy.types import PolicyDecision, Verdict

# Cache the rules on first load
_RULES_CACHE: dict[str, Any] | None = None


def _load_rules() -> dict[str, Any]:
    """Load jurisdiction rules from rules.yaml."""
    global _RULES_CACHE
    if _RULES_CACHE is not None:
        return _RULES_CACHE

    rules_path = Path(__file__).parent / "rules.yaml"
    with open(rules_path) as f:
        data: dict[str, Any] = yaml.safe_load(f)
    _RULES_CACHE = data
    return data


async def evaluate(company: Company, session: AsyncSession) -> PolicyDecision:
    """Evaluate a company against jurisdiction rules.

    For the UK, this also reads the entity_type fact from the database to
    determine if the company is a corporate entity (exempt from consent) or
    needs consent.

    Args:
        company: The company to evaluate
        session: Database session

    Returns:
        PolicyDecision with verdict, requirements, and plain English reason
    """
    rules = _load_rules()
    country_code = company.country_code.upper()

    # Get the base rule for this country
    jurisdiction_rule = rules.get("jurisdictions", {}).get(
        country_code,
        rules.get("default"),
    )

    if not jurisdiction_rule:
        # Fallback to default if nothing found
        jurisdiction_rule = rules.get("default", {})

    # Special handling for UK: check entity_type from facts
    if country_code == "UK":
        entity_type = await _get_entity_type(str(company.id), session)
        if entity_type in ("ltd", "plc"):
            # Corporate entity: use the ALLOW verdict
            pass
        elif entity_type in ("sole_trader", "partnership"):
            # Individual or partnership: upgrade to WARN
            # Keep the base rule but change verdict to WARN
            jurisdiction_rule = dict(jurisdiction_rule)
            jurisdiction_rule["verdict"] = "warn"

    verdict_str = jurisdiction_rule.get("verdict", "warn")
    try:
        verdict = Verdict(verdict_str)
    except ValueError:
        verdict = Verdict.WARN

    requirements = []
    for req_dict in jurisdiction_rule.get("requirements", []):
        if isinstance(req_dict, dict):
            for _key, val in req_dict.items():
                requirements.append(val)

    reason = jurisdiction_rule.get(
        "reason",
        f"No rule found for {country_code}; assuming conservative compliance",
    )

    return PolicyDecision(
        verdict=verdict,
        requirements=requirements,
        reason=reason,
    )


async def _get_entity_type(company_id: str, session: AsyncSession) -> str | None:
    """Look up entity_type fact for a company (UK companies house data).

    Returns: "ltd", "plc", "sole_trader", "partnership", or None if not found.
    """
    row = await session.execute(
        select(Fact).where(
            Fact.subject_type == SubjectType.COMPANY,
            Fact.subject_id == company_id,
            Fact.field == "entity_type",
        )
    )
    fact = row.scalars().first()
    return fact.value if fact else None


__all__ = ["PolicyDecision", "Verdict", "evaluate"]
