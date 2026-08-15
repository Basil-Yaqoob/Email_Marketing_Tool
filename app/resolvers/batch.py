"""run_batch — the anti-silent-failure guard.

This is the direct fix for the prototype's actual bug: 1,640 consecutive
verification failures recorded as the string "error", a results file
written, a success summary printed, exit code 0. Two abort conditions,
both raising BatchAbortedError instead of finishing quietly:

  1. Error rate exceeds error_threshold, checked only after min_checked
     leads so two early failures don't abort a large run on noise.
  2. The batch completes with zero hits and zero errors. That is the exact
     shape of the original bug — nothing raised, and nothing worked either.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.core.errors import BatchAbortedError
from app.db.models.enums import ResolverOutcome
from app.resolvers.base import LeadContext
from app.resolvers.executor import Resolution, run_waterfall
from app.resolvers.registry import ResolverRegistry


@dataclass(frozen=True, slots=True)
class BatchResult:
    resolutions: list[Resolution]
    checked: int
    hits: int
    errors: int


async def run_batch(
    *,
    field: str,
    contexts: Sequence[LeadContext],
    registry: ResolverRegistry,
    threshold: float = 0.85,
    allow_metered: bool = False,
    error_threshold: float = 0.05,
    min_checked: int = 20,
) -> BatchResult:
    resolutions: list[Resolution] = []
    checked = 0
    hit_count = 0
    error_count = 0

    for ctx in contexts:
        checked += 1
        resolution = await run_waterfall(
            field=field,
            ctx=ctx,
            registry=registry,
            threshold=threshold,
            allow_metered=allow_metered,
        )
        resolutions.append(resolution)

        if any(a.outcome == ResolverOutcome.ERROR for a in resolution.attempts):
            error_count += 1
        if resolution.best is not None:
            hit_count += 1

        if checked >= min_checked:
            error_rate = error_count / checked
            if error_rate > error_threshold:
                raise BatchAbortedError(
                    completed=checked,
                    total=len(contexts),
                    error_rate=error_rate,
                    last=_last_error(resolution) or "unknown error",
                )

    if checked > 0 and hit_count == 0 and error_count == 0:
        raise BatchAbortedError(
            completed=checked,
            total=len(contexts),
            error_rate=0.0,
            last="zero yield: every lead completed with no candidates and no errors",
        )

    return BatchResult(resolutions=resolutions, checked=checked, hits=hit_count, errors=error_count)


def _last_error(resolution: Resolution) -> str | None:
    errors = [
        a.error for a in resolution.attempts if a.outcome == ResolverOutcome.ERROR and a.error
    ]
    return errors[-1] if errors else None
