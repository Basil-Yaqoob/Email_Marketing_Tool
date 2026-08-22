"""Construction of the long-lived components, in one place.

Until now nothing in `app/` built an HttpClient or an LLMGateway -- only
tests did, each assembling its own. That is why the application had 19,000
lines of tested components and no running pipeline: every piece existed
and nothing wired them together.

Everything here is expensive to build and safe to share: an HttpClient
holds a connection pool, a rate limiter, and an on-disk cache, so building
one per lead would defeat all three. The worker builds these once at
startup and hands them to each pipeline run.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from decimal import Decimal

from app.core.config import Settings
from app.llm.cost import CostTracker, InMemoryCostTracker, SpendCap
from app.llm.gateway import LLMGateway
from app.llm.prices import PriceTable
from app.llm.providers.registry import build_providers
from app.net.cache import ResponseCache
from app.net.client import HttpClient
from app.net.ratelimit import RateLimiter
from app.net.robots import RobotsChecker
from app.sending.credentials import CredentialVault

# Identifies us honestly to every site we fetch. A crawler that hides what
# it is cannot be asked to stop, which is the point of the contact URL.
USER_AGENT = (
    "EmailMarketingTool/0.1 (+https://github.com/self-hosted-cold-email; "
    "self-hosted, contact the operator of this instance)"
)

# Short enough to be typeable, long enough that a brute force against the
# derived Fernet key is not the weak link.
_MIN_SECRET_LENGTH = 16


def build_http_client(settings: Settings) -> HttpClient:
    """One client per worker process, not one per lead.

    The response cache in particular only pays for itself when it is
    shared: re-parsing a page a later stage already fetched should never
    cost a second request.
    """
    return HttpClient(
        cache=ResponseCache(settings.http_cache_dir),
        limiter=RateLimiter(),
        robots=RobotsChecker(user_agent=USER_AGENT),
        user_agent=USER_AGENT,
    )


def build_credential_vault(settings: Settings) -> CredentialVault:
    """Derives the mailbox-credential key from SECRET_KEY.

    SECRET_KEY is a general-purpose secret -- it also signs unsubscribe
    tokens -- so it is not required to be Fernet-shaped. The Fernet key is
    derived from it by SHA-256, which always yields exactly the 32 bytes
    Fernet wants from a secret of any length.

    Derivation is deterministic on purpose. Generating a key instead would
    produce a new one every restart and silently make every stored mailbox
    credential undecryptable, surfacing much later as "your mailbox
    password is wrong" rather than as a config error.

    Rotating SECRET_KEY therefore invalidates stored mailbox credentials
    and any unsent unsubscribe token; both have to be re-entered.
    """
    secret = settings.secret_key.get_secret_value()
    if len(secret) < _MIN_SECRET_LENGTH:
        raise ValueError(
            f"SECRET_KEY must be at least {_MIN_SECRET_LENGTH} characters; it encrypts "
            "mailbox credentials at rest. Generate one with:\n"
            '  python -c "import secrets; print(secrets.token_urlsafe(32))"'
        )
    derived = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
    return CredentialVault(derived)


def build_llm_gateway(
    settings: Settings, *, tracker: CostTracker | None = None
) -> LLMGateway | None:
    """The gateway, or None when no provider key is configured.

    None rather than a stub: the stages that need an LLM (hook mining,
    copywriting) must be able to say "not configured" and be skipped,
    rather than run against something that silently returns nothing.
    """
    providers = build_providers(settings)
    if not providers:
        return None

    cost_tracker = tracker or InMemoryCostTracker()
    spend_cap = (
        SpendCap(limit_usd=Decimal(settings.llm_spend_cap_usd), tracker=cost_tracker)
        if settings.llm_spend_cap_usd is not None
        else None
    )
    return LLMGateway(
        providers=providers,
        prices=PriceTable.load(),
        tracker=cost_tracker,
        model_overrides=settings.llm_model_overrides,
        spend_cap=spend_cap,
    )


@dataclass(slots=True)
class Runtime:
    """The shared, process-lifetime components a pipeline run needs.

    Bundled so a stage signature stays readable and so the worker has one
    obvious thing to build at startup and close at shutdown.
    """

    settings: Settings
    http: HttpClient
    vault: CredentialVault
    cost_tracker: CostTracker
    llm: LLMGateway | None

    @property
    def llm_available(self) -> bool:
        return self.llm is not None

    async def aclose(self) -> None:
        await self.http.aclose()


async def build_runtime(settings: Settings | None = None) -> Runtime:
    resolved = settings or Settings()
    tracker = InMemoryCostTracker()
    return Runtime(
        settings=resolved,
        http=build_http_client(resolved),
        vault=build_credential_vault(resolved),
        cost_tracker=tracker,
        llm=build_llm_gateway(resolved, tracker=tracker),
    )


__all__ = [
    "USER_AGENT",
    "Runtime",
    "build_credential_vault",
    "build_http_client",
    "build_llm_gateway",
    "build_runtime",
]
