"""Per-campaign spend cap across all metered MCP resolvers.

CLAUDE.md rule 2.3: free before paid, always. This is the enforcement point
for the "always" -- a cap that could have been exceeded and wasn't checked
is a bug, not an edge case. Raises, never a silent continue past the limit
the user set (mirrors app.llm.cost's spend cap for the same reason).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from app.core.errors import BudgetExceededError


@dataclass(slots=True)
class MCPBudget:
    """Tracks spend for one campaign across every MCPResolver that shares
    this instance. A single MCPBudget is constructed per campaign run and
    passed to every registered MCP resolver -- Apollo and Clay calls both
    count against the same cap, since the plan is "spend across all metered
    MCP resolvers", not one cap per vendor.
    """

    campaign_id: str
    cap: Decimal
    spent: Decimal = field(default=Decimal("0"))

    def record(self, cost: Decimal, *, resolver_name: str) -> None:
        """Charge `cost` against the cap. Raises BudgetExceededError and
        leaves `spent` unchanged if the charge would exceed the cap -- the
        call that would have exceeded it must not be made at all, so this
        is checked by the resolver *before* calling the tool, not after.
        """
        if self.spent + cost > self.cap:
            raise BudgetExceededError(
                f"MCP spend cap exceeded for campaign {self.campaign_id}: "
                f"{resolver_name} call would bring spend to "
                f"${self.spent + cost:.2f}, cap is ${self.cap:.2f}"
            )
        self.spent += cost

    def remaining(self) -> Decimal:
        return self.cap - self.spent


def project_spend(*, n_leads_unresolved: int, cost_per_call: Decimal) -> Decimal:
    """Projected worst-case spend if every unresolved lead needs one paid
    call. Shown to the user before a run starts them -- they should know
    they are about to spend this before they spend it, not after.
    """
    return Decimal(n_leads_unresolved) * cost_per_call


def format_spend_projection(
    *, n_leads_unresolved: int, cost_per_call: Decimal, resolver_name: str
) -> str:
    """Human-readable projection line for a pre-run confirmation prompt."""
    total = project_spend(n_leads_unresolved=n_leads_unresolved, cost_per_call=cost_per_call)
    return (
        f"{resolver_name}: up to {n_leads_unresolved} leads x ${cost_per_call:.2f}/call "
        f"= ${total:.2f} projected"
    )


__all__ = ["MCPBudget", "format_spend_projection", "project_spend"]
