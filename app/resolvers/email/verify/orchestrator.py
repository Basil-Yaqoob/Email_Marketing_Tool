"""The ladder, and the guard that makes it fail loudly.

This module exists because of one specific incident. The prototype's
verification stage ran 1,640 calls against a vendor whose credits had run
out, caught every failure with `except Exception`, returned the string
"error", wrote its results file, printed a success summary, and exited 0.
Everything downstream was worthless and nobody knew for weeks.

Two guards, both here:

1. **Error rate.** Failures raise VerificationError and are counted. Past
   the threshold the batch aborts with BatchAbortedError. A failure is
   never converted into an outcome — there is no VerifyStatus member it
   could be written as (see models.py).

2. **Zero yield.** A batch that completes with no errors *and* not a
   single VALID result also aborts. This is the subtler version of the
   same bug: every individual result looked legitimate, and the aggregate
   is still worthless. See verify_batch's docstring for the one honest
   false positive this has.

UNKNOWN and CATCH_ALL are legitimate, permanent answers and never count
toward the error rate.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.core.errors import BatchAbortedError, VerificationError
from app.core.logging import get_logger
from app.db.models.enums import VerifyStatus
from app.resolvers.email.learning import ConfirmationSource, LearnedPattern, learn_from_confirmed
from app.resolvers.email.verify.models import MailProvider, Rung, VerificationResult
from app.resolvers.email.verify.mx import DnsResolver, lookup_mx
from app.resolvers.email.verify.smtp_probe import SMTPProber
from app.resolvers.email.verify.syntax import check_syntax, split_address

log = get_logger(__name__)

DEFAULT_ERROR_THRESHOLD = 0.05
DEFAULT_MIN_CHECKED = 20


@dataclass(frozen=True, slots=True)
class BatchResult:
    results: tuple[VerificationResult, ...]
    error_count: int
    learned: tuple[LearnedPattern, ...] = field(default_factory=tuple)

    @property
    def checked(self) -> int:
        return len(self.results)

    def count(self, status: VerifyStatus) -> int:
        return sum(1 for r in self.results if r.status == status)

    @property
    def error_rate(self) -> float:
        total = self.checked + self.error_count
        return self.error_count / total if total else 0.0


async def verify_one(
    address: str,
    *,
    resolver: DnsResolver,
    prober: SMTPProber | None = None,
    now: Callable[[], datetime] | None = None,
) -> VerificationResult:
    """Walk the ladder until something settles the address.

    `prober=None` means rung 3 is not deployed. That degrades to UNKNOWN —
    an honest "we could not check this" — rather than crashing the
    pipeline or, worse, guessing. The prober is a separate service by
    design so the main app keeps working without it.
    """
    clock = now or (lambda: datetime.now(UTC))

    settled = check_syntax(address)
    if settled is not None:
        return VerificationResult(
            address=address,
            status=settled,
            rung=Rung.SYNTAX,
            provider=MailProvider.NONE,
            checked_at=clock(),
        )

    _, domain = split_address(address)
    mx = await lookup_mx(domain, resolver)

    if not mx.has_mx:
        return VerificationResult(
            address=address,
            status=VerifyStatus.INVALID,
            rung=Rung.MX,
            provider=MailProvider.NONE,
            checked_at=clock(),
            detail="domain has no MX records and accepts no mail",
        )

    if mx.provider.blocks_smtp_verification:
        # CLAUDE.md §10: this is a stated, permanent limit of the product,
        # not a bug to work around. Probing here would return 250 for
        # addresses that do not exist.
        return VerificationResult(
            address=address,
            status=VerifyStatus.UNKNOWN,
            rung=Rung.MX,
            provider=mx.provider,
            checked_at=clock(),
            detail=f"{mx.provider.value} accepts RCPT TO for nonexistent mailboxes",
        )

    if prober is None:
        return VerificationResult(
            address=address,
            status=VerifyStatus.UNKNOWN,
            rung=Rung.MX,
            provider=mx.provider,
            checked_at=clock(),
            detail="no SMTP prober configured",
        )

    status = await prober.probe(address, list(mx.hosts))
    return VerificationResult(
        address=address,
        status=status,
        rung=Rung.SMTP,
        provider=mx.provider,
        checked_at=clock(),
    )


async def verify_batch(
    addresses: Sequence[str],
    *,
    resolver: DnsResolver,
    prober: SMTPProber | None = None,
    names: Mapping[str, str] | None = None,
    error_threshold: float = DEFAULT_ERROR_THRESHOLD,
    min_checked: int = DEFAULT_MIN_CHECKED,
    now: Callable[[], datetime] | None = None,
) -> BatchResult:
    """Run the ladder over many addresses, aborting loudly on failure.

    Aborts when the error rate exceeds `error_threshold` (checked once at
    least `min_checked` addresses have been attempted, so a single early
    failure in a tiny batch doesn't trip it), and when a batch of at least
    `min_checked` completes with zero VALID results and zero errors.

    **Known false positive on the zero-yield guard:** a batch whose
    domains are genuinely all Google- or Microsoft-hosted resolves to all
    UNKNOWN with no errors and will abort, even though nothing is broken.
    That is the deliberate trade — a zero-yield run is nearly always a
    dead prober or a misconfiguration, and the cost of stopping to ask is
    far below the cost of the prototype's silent zero. The operator's
    remedy is to confirm the provider mix and lower `min_checked` or raise
    the batch's diversity, not to remove the guard.

    When `names` maps an address to its owner's full name, a VALID result
    also feeds Session 09's domain pattern learning at VERIFICATION
    strength — a confirmed address is exactly the signal that turns nine
    future guesses at that domain into one.
    """
    results: list[VerificationResult] = []
    learned: list[LearnedPattern] = []
    error_count = 0
    last_error = ""

    for index, address in enumerate(addresses, start=1):
        try:
            result = await verify_one(address, resolver=resolver, prober=prober, now=now)
        except VerificationError as exc:
            # Counted, logged, and never written to the lead as an answer.
            error_count += 1
            last_error = str(exc)
            log.warning("verify.error", address=address, error=last_error)
        else:
            results.append(result)
            if result.status == VerifyStatus.VALID and names:
                full_name = names.get(address)
                if full_name:
                    pattern = learn_from_confirmed(
                        address, full_name, source=ConfirmationSource.VERIFICATION
                    )
                    if pattern is not None:
                        learned.append(pattern)

        attempted = index
        if attempted >= min_checked:
            rate = error_count / attempted
            if rate > error_threshold:
                raise BatchAbortedError(
                    completed=len(results),
                    total=len(addresses),
                    error_rate=rate,
                    last=last_error or "unknown",
                )

    batch = BatchResult(results=tuple(results), error_count=error_count, learned=tuple(learned))

    if batch.checked >= min_checked and error_count == 0 and batch.count(VerifyStatus.VALID) == 0:
        raise BatchAbortedError(
            completed=batch.checked,
            total=len(addresses),
            error_rate=0.0,
            last=(
                f"zero VALID results across {batch.checked} checked addresses with no errors "
                f"(invalid={batch.count(VerifyStatus.INVALID)}, "
                f"unknown={batch.count(VerifyStatus.UNKNOWN)}, "
                f"catch_all={batch.count(VerifyStatus.CATCH_ALL)}, "
                f"role={batch.count(VerifyStatus.ROLE)}) — "
                "a dead prober and a genuinely unverifiable batch look identical from here"
            ),
        )

    return batch
