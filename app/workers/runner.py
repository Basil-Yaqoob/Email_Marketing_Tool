"""The worker loop — the thing `app/workers/__init__.py` has been an empty
file in place of since the project started.

Claims a job, runs the matching pipeline stage against a fresh database
session, and records the outcome. One job at a time on purpose: the
stages are already internally concurrent (enrichment crawls 8 sites at
once), and running several stages in parallel against one machine's
network and DNS would make every one of them slower.

The three terminal outcomes are kept distinct because they mean different
things to the person looking at the screen:

  SUCCEEDED  the stage ran and finished, whatever it found
  ABORTED    a guard stopped it on purpose; the reason is recorded verbatim
  FAILED     something crashed

Collapsing ABORTED into FAILED would hide the one signal this product
exists to surface -- a run stopped *because* the error rate spiked is the
system working, and it has to read differently from a bug.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid

import structlog
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.errors import BatchAbortedError
from app.core.logging import configure_logging
from app.services.email_resolution import run_email_resolution, run_verification
from app.services.enrichment import run_enrichment
from app.services.factories import Runtime, build_runtime
from app.services.jobs import RedisJobQueue, Stage, build_job_queue
from app.services.pipeline import StageResult, run_discovery

log = structlog.get_logger(__name__)


async def _run_stage(
    stage: Stage, session: object, runtime: Runtime, campaign_id: uuid.UUID
) -> StageResult:
    """Dispatch to the stage implementation.

    Raises on an unimplemented stage rather than returning an empty result
    -- a stage that silently does nothing and reports success is the exact
    failure this codebase is built to avoid.
    """
    if stage is Stage.DISCOVERY:
        return await run_discovery(session, runtime, campaign_id)  # type: ignore[arg-type]
    if stage is Stage.ENRICHMENT:
        return await run_enrichment(session, runtime, campaign_id)  # type: ignore[arg-type]
    if stage is Stage.EMAIL_RESOLUTION:
        return await run_email_resolution(session, runtime, campaign_id)  # type: ignore[arg-type]
    if stage is Stage.VERIFICATION:
        return await run_verification(session, runtime, campaign_id)  # type: ignore[arg-type]
    raise NotImplementedError(
        f"stage {stage.value!r} is not implemented yet — it needs the LLM "
        "stages, which need a campaign offer and a search backend configured"
    )


async def process_one(
    payload: dict[str, str],
    *,
    queue: RedisJobQueue,
    runtime: Runtime,
    session_factory: async_sessionmaker,  # type: ignore[type-arg]
) -> None:
    job_id = uuid.UUID(payload["job_id"])
    campaign_id = uuid.UUID(payload["campaign_id"])
    stage = Stage(payload["stage"])

    await queue.mark_running(job_id)
    log.info("job.started", job_id=str(job_id), stage=stage.value)

    try:
        async with session_factory() as session:
            result = await _run_stage(stage, session, runtime, campaign_id)
            await session.commit()
    except BatchAbortedError as exc:
        # A guard fired. Not a crash: the run was stopped deliberately
        # because something looked systemically wrong, and the reason is
        # preserved verbatim so it can be diagnosed without re-running.
        await queue.mark_aborted(job_id, str(exc))
        log.warning("job.aborted", job_id=str(job_id), stage=stage.value, reason=str(exc))
        return
    except Exception as exc:  # noqa: BLE001
        # Deliberately broad, and recorded rather than swallowed. This is
        # the top of a long-lived loop: letting one job's crash propagate
        # would kill the worker and silently strand every job queued
        # behind it. The exception is written to the job record, so it
        # surfaces in the UI rather than only in a log line.
        await queue.mark_failed(job_id, exc)
        log.error("job.failed", job_id=str(job_id), stage=stage.value, error=str(exc))
        return

    await queue.mark_complete(job_id, processed=result.attempted, total=max(result.attempted, 1))
    log.info(
        "job.completed",
        job_id=str(job_id),
        stage=stage.value,
        found=result.found,
        written=result.written,
    )


async def worker_loop(*, poll_timeout: int = 5) -> None:
    """Run until cancelled."""
    runtime = await build_runtime()
    configure_logging(runtime.settings)
    queue = build_job_queue(runtime.settings.redis_url)
    engine = create_async_engine(runtime.settings.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    log.info("worker.started")
    try:
        while True:
            payload = await queue.claim(timeout=poll_timeout)
            if payload is None:
                continue
            await process_one(
                payload, queue=queue, runtime=runtime, session_factory=session_factory
            )
    finally:
        await runtime.aclose()
        await engine.dispose()
        log.info("worker.stopped")


def main() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(worker_loop())


if __name__ == "__main__":
    main()
