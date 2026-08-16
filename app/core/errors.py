"""Exception hierarchy for everything this application raises deliberately.

CLAUDE.md rule 2.1: a stage that cannot do its job raises. It never returns
a sentinel value or swallows the failure. Every exception here exists because
a real failure mode in the prototype went unnoticed until it was expensive.
"""

from __future__ import annotations


class AppError(Exception):
    """Base for everything this application raises deliberately."""


class MissingConfigError(AppError):
    """A required setting has no value and no default.

    Raised at startup, not at first use — a missing key should fail before
    the app accepts any work, not three hours into a campaign run.
    """

    def __init__(self, key: str) -> None:
        super().__init__(
            f"Required configuration '{key}' is not set. Add it to .env — there is no default."
        )
        self.key = key


class BatchAbortedError(AppError):
    """Raised when a batch's error rate crosses the configured threshold.

    This exists because the prototype recorded 1,640 consecutive failures
    as the string "error", wrote a results file, printed a success summary,
    and exited 0. A guard that fires deliberately must say so loudly, with
    enough detail to diagnose at 2am without re-running anything.
    """

    def __init__(self, *, completed: int, total: int, error_rate: float, last: str) -> None:
        super().__init__(
            f"Batch aborted after {completed}/{total}: "
            f"error rate {error_rate:.1%} exceeded threshold. Last error: {last}"
        )
        self.completed = completed
        self.total = total
        self.error_rate = error_rate
        self.last = last


class ResolverError(AppError):
    """Base for failures inside a waterfall resolver."""


class UpstreamError(ResolverError):
    """The remote source failed on its side: 5xx, timeout, malformed response."""


class RateLimitedError(ResolverError):
    """The remote source asked us to back off. Retry, don't treat as failure."""


class ParseError(ResolverError):
    """Our fault: the page or response shape changed under us."""


class LLMError(AppError):
    """Base for failures in the LLM gateway."""


class SchemaValidationError(LLMError):
    """The model's output did not satisfy the requested schema, and the
    self-correcting retry did not fix it.

    Raised, never softened into a partial object or a None. CLAUDE.md rule
    2.1: a stage that cannot do its job raises. A half-parsed strategy
    object silently missing its `angle` field is exactly the kind of
    degraded output that produces confidently wrong copy downstream.
    """


class ContentRefusalError(LLMError):
    """The model declined to answer.

    Distinct from a transport failure and deliberately **never retried**:
    the same prompt will be refused again, so retrying only burns tokens
    and time. This is a prompt problem for a human to look at.
    """


class ModelNotFoundError(LLMError):
    """The configured model id does not exist at the provider.

    OpenRouter deprecates and renames model ids regularly, and the failure
    arrives as a generic 404. Naming it explicitly is the difference
    between "your config points at a retired model" and an hour of
    debugging a network error that isn't one.
    """


class NoProviderConfiguredError(LLMError):
    """No provider key is configured, so nothing can serve this call.

    Providers whose key is absent are simply not registered (see
    app/llm/providers/registry.py). Reaching this means *none* were.
    """


class VerificationError(AppError):
    """A verification check could not be completed — OUR failure, never a
    lead's answer.

    This exception is the entire fix for the prototype's worst bug. That
    code caught a failing vendor call with `except Exception`, returned
    the string "error", and wrote it to the lead as if it were a
    verification result — 1,640 times, then exited 0.

    Here the failure has nowhere to hide: there is no ERROR member of
    VerifyStatus (app/db/models/enums.py) for it to be written as, so a
    system failure can only propagate as this exception. The batch runner
    counts these and aborts past a threshold; it never converts one into
    an outcome.
    """


class RobotsDisallowedError(AppError):
    """robots.txt disallows fetching this URL for our user agent.

    Raised by app.net.client.HttpClient, not silently skipped — a caller
    that wants to override site policy must pass respect_robots=False
    explicitly, per-call. Not legally binding in most places, but ignoring
    it by default is how a scraper gets IP-banned.
    """

    def __init__(self, url: str) -> None:
        super().__init__(f"robots.txt disallows fetching {url}")
        self.url = url


class PolicyBlockedError(AppError):
    """A send was blocked by the compliance engine (CLAUDE.md rule 2.5 territory).

    Never a warning — a template missing a physical address or unsubscribe
    link does not go out, full stop.
    """


class BudgetExceededError(AppError):
    """A metered spend cap was reached.

    CLAUDE.md rule 2.3: a metered call that could have been avoided is a bug.
    This is the enforcement point — raised, never a silent continue past the
    cap the user set.
    """
