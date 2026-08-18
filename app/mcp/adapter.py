"""MCPResolver -- turns any MCP tool into a waterfall Resolver.

This is the actual deliverable of Session 22: Apollo and Clay
(app/mcp/servers/apollo.py, clay.py) are just the first two MCPResolverConfig
values, not special-cased code. A third vendor's MCP server becomes a
resolver by writing one more config, not by writing a class.

Unlike every other resolver in this codebase, name/field/tier are instance
attributes rather than class attributes -- one MCPResolver class is
instantiated once per (server, tool, field) combination, since a single
server can serve several fields (person_name, email, company_size, ...)
each needing its own field mapping and confidence calibration.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.mcp.budget import MCPBudget
from app.mcp.client import MCPClient
from app.mcp.errors import MCPServerUnavailableError
from app.mcp.types import MCPResult, MCPServerConfig, MCPSession
from app.resolvers.base import BaseResolver, Candidate, LeadContext, Tier

# LeadContext attributes a mapping may read directly, as opposed to falling
# back to ctx.known_facts[context_attr]. company_id is deliberately excluded
# -- it's an internal UUID, never something a third-party tool needs.
_DIRECT_CONTEXT_FIELDS = frozenset(
    {"company_name", "domain", "website", "country_code", "person_name"}
)


@dataclass(frozen=True, slots=True)
class FieldMapping:
    """One argument to send to the MCP tool.

    Read from a LeadContext attribute when `context_attr` is one of
    company_name/domain/website/country_code/person_name; otherwise read
    from `ctx.known_facts[context_attr]`. Omitted from the call entirely
    (not sent as null) when the source value is missing, unless `required`
    is set, in which case a missing value means this resolver isn't
    applicable to the lead.
    """

    tool_arg: str
    context_attr: str
    required: bool = False


@dataclass(frozen=True, slots=True)
class ResultMapping:
    """How to turn one item of a tool's result into a Candidate.

    CLAUDE.md: never import someone else's "verified" as our VALID without
    checking what they mean by it. When the server ships its own
    verification vocabulary, set `verification_key` (the field carrying
    their label) and `verified_value_map` (their label -> our confidence,
    calibrated deliberately -- e.g. {"verified": 0.9, "guessed": 0.4}, not
    {"verified": 1.0} on the assumption their "verified" matches our SMTP
    probe). Sources with no verification concept at all use
    `confidence_key`/`default_confidence` instead.
    """

    value_key: str
    source_url_key: str | None = None
    confidence_key: str | None = None
    default_confidence: float = 0.5
    verification_key: str | None = None
    verified_value_map: Mapping[str, float] | None = None


@dataclass(frozen=True, slots=True)
class MCPResolverConfig:
    """Everything needed to run one MCP tool as one waterfall resolver."""

    name: str
    field: str
    server: MCPServerConfig
    tool_name: str
    cost_per_call: Decimal
    context_mapping: tuple[FieldMapping, ...]
    result_mapping: ResultMapping
    # Set when the tool's result is a container, e.g. {"people": [...]} ->
    # "people". Left None when the result is either a single object or
    # already a bare list.
    result_list_key: str | None = None
    jurisdictions: frozenset[str] | None = None
    budget: MCPBudget | None = None


class MCPResolver(BaseResolver):
    """Wraps one MCP tool as a waterfall resolver, driven entirely by an
    MCPResolverConfig -- see the module docstring.
    """

    tier = Tier.METERED

    def __init__(self, config: MCPResolverConfig, client: MCPClient) -> None:
        self.name = config.name
        self.field = config.field
        self.cost_per_call = config.cost_per_call
        self.jurisdictions = config.jurisdictions
        self._config = config
        self._client = client
        self._session: MCPSession | None = None

    async def applicable(self, ctx: LeadContext) -> bool:
        if not await super().applicable(ctx):
            return False
        for mapping in self._config.context_mapping:
            if mapping.required and _read_context_value(ctx, mapping.context_attr) is None:
                return False
        return True

    async def resolve(self, ctx: LeadContext) -> list[Candidate]:
        args = self._build_args(ctx)
        result = await self._call_with_reconnect(args)

        # Budget is charged only after a successful call -- a call that
        # raised never happened as far as spend is concerned. Checked
        # *before* the call would be made would need knowing the cost
        # up front, which this does (cost_per_call is static), but a call
        # that fails still consumes the vendor's quota in practice, so
        # charging on success keeps our recorded spend from drifting from
        # the vendor's own -- undercounting is the safer direction for a
        # cap meant to prevent overspend.
        if self._config.budget is not None:
            self._config.budget.record(self._config.cost_per_call, resolver_name=self.name)

        return self._map_result(result)

    async def _call_with_reconnect(self, args: dict[str, Any]) -> MCPResult:
        timeout = self._config.server.timeout_seconds
        session = await self._get_session()
        try:
            return await self._client.call_tool(
                session, self._config.tool_name, args, timeout_seconds=timeout
            )
        except MCPServerUnavailableError:
            # The cached session may have gone stale (server restarted, a
            # network blip). One reconnect-and-retry; a second failure
            # propagates to the caller (app.resolvers.executor._run_one),
            # which records it as this attempt's ERROR outcome rather than
            # crashing the batch.
            self._session = None
            session = await self._get_session()
            return await self._client.call_tool(
                session, self._config.tool_name, args, timeout_seconds=timeout
            )

    async def _get_session(self) -> MCPSession:
        if self._session is None:
            self._session = await self._client.connect(self._config.server)
        return self._session

    def _build_args(self, ctx: LeadContext) -> dict[str, Any]:
        args: dict[str, Any] = {}
        for mapping in self._config.context_mapping:
            value = _read_context_value(ctx, mapping.context_attr)
            if value is not None:
                args[mapping.tool_arg] = value
        return args

    def _map_result(self, result: MCPResult) -> list[Candidate]:
        return [
            candidate
            for item in self._result_items(result)
            if (candidate := self._map_item(item)) is not None
        ]

    def _result_items(self, result: MCPResult) -> list[Mapping[str, Any]]:
        data = result.data
        if data is None or isinstance(data, str):
            return []  # unstructured text result -- nothing to map generically
        if self._config.result_list_key is not None:
            items = data.get(self._config.result_list_key, []) if isinstance(data, Mapping) else []
            return [i for i in items if isinstance(i, Mapping)]
        if isinstance(data, list):
            return [i for i in data if isinstance(i, Mapping)]
        if isinstance(data, Mapping):
            return [data]
        return []

    def _map_item(self, item: Mapping[str, Any]) -> Candidate | None:
        rm = self._config.result_mapping
        value = item.get(rm.value_key)
        if not value:
            return None

        confidence = self._resolve_confidence(rm, item)
        source_url = item.get(rm.source_url_key) if rm.source_url_key else None

        return Candidate(
            value=str(value),
            confidence=min(max(confidence, 0.0), 1.0),
            source=self.name,
            source_url=str(source_url) if source_url else None,
            # Candidates from the same MCP server are correlated (one
            # vendor's data pipeline, not independent evidence) -- shared
            # independence_key collapses them to their best member during
            # merge instead of letting them stack into inflated confidence.
            independence_key=f"mcp:{self._config.server.name}",
            extra={"raw": dict(item)},
        )

    def _resolve_confidence(self, rm: ResultMapping, item: Mapping[str, Any]) -> float:
        if rm.verification_key is not None and rm.verified_value_map is not None:
            label = item.get(rm.verification_key)
            if isinstance(label, str) and label in rm.verified_value_map:
                return rm.verified_value_map[label]
            return rm.default_confidence
        if rm.confidence_key is not None and rm.confidence_key in item:
            try:
                return float(item[rm.confidence_key])
            except (TypeError, ValueError):
                return rm.default_confidence
        return rm.default_confidence


def _read_context_value(ctx: LeadContext, attr: str) -> str | None:
    if attr in _DIRECT_CONTEXT_FIELDS:
        return getattr(ctx, attr)  # type: ignore[no-any-return]
    return ctx.known_facts.get(attr)


__all__ = ["FieldMapping", "MCPResolver", "MCPResolverConfig", "ResultMapping"]
