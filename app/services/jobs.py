"""Job queue — how a web request starts work that takes minutes.

Discovery takes a minute against live OSM; crawling 65 sites takes several
even with concurrency. An HTTP handler that awaits either is a handler
that times out, so every pipeline stage is enqueued and the caller polls.

State lives in Redis rather than process memory for two reasons: the UI
polls from a different request than the one that started the job, and a
worker restart must not silently lose a running job's identity. A job whose
worker died is visible as one that stopped updating, not as one that never
existed.

`JobState` and `JobStatus` are the existing Session 20 API models, reused
rather than re-invented -- notably ABORTED, which carries the guard's
reason verbatim and is deliberately distinct from FAILED. A batch the
error-rate guard stopped on purpose is not the same event as a crash, and
the UI has to be able to say which happened.
"""

from __future__ import annotations

import enum
import json
import uuid
from datetime import UTC, datetime
from typing import Any, Protocol

import redis.asyncio as redis
import structlog

from app.api.models import JobError, JobState, JobStatus

log = structlog.get_logger(__name__)

# Long enough to still be readable the morning after an overnight run,
# short enough that Redis does not accumulate job records forever.
JOB_TTL_SECONDS = 60 * 60 * 24 * 7

_KEY = "job:{job_id}"
_QUEUE = "jobs:pending"


class Stage(enum.StrEnum):
    """The pipeline stages a caller can run.

    A StrEnum so an unknown stage name fails at the boundary with a clear
    ValueError rather than being passed down and silently doing nothing.
    """

    DISCOVERY = "discovery"
    ENRICHMENT = "enrichment"
    EMAIL_RESOLUTION = "email_resolution"
    VERIFICATION = "verification"
    HOOK_RESEARCH = "hook_research"
    COPY = "copy"


class JobQueue(Protocol):
    async def enqueue(self, stage: Stage, campaign_id: uuid.UUID) -> JobStatus: ...
    async def status(self, job_id: uuid.UUID) -> JobStatus: ...


def _encode(status: JobStatus) -> str:
    return status.model_dump_json()


def _decode(raw: str) -> JobStatus:
    return JobStatus.model_validate_json(raw)


class RedisJobQueue:
    """Redis-backed queue and status store.

    Deliberately small: a list for pending work and one key per job. arq is
    already a dependency for scheduled work, but a hand-rolled queue here
    keeps the job *record* in a shape the UI and the MCP server can both
    read directly, rather than behind arq's internal result format.
    """

    def __init__(self, client: redis.Redis) -> None:
        self._redis = client

    async def enqueue(self, stage: Stage, campaign_id: uuid.UUID) -> JobStatus:
        job = JobStatus(
            id=uuid.uuid4(),
            state=JobState.QUEUED,
            progress=0.0,
            processed=0,
            total=0,
            created_at=datetime.now(UTC),
        )
        payload = {
            "job_id": str(job.id),
            "stage": stage.value,
            "campaign_id": str(campaign_id),
        }
        await self._redis.set(_KEY.format(job_id=job.id), _encode(job), ex=JOB_TTL_SECONDS)
        await self._redis.rpush(_QUEUE, json.dumps(payload))  # type: ignore[misc]
        log.info("job.enqueued", job_id=str(job.id), stage=stage.value)
        return job

    async def status(self, job_id: uuid.UUID) -> JobStatus:
        raw = await self._redis.get(_KEY.format(job_id=job_id))
        if raw is None:
            # Distinguishable from "still queued": a job id that has no
            # record either never existed or aged out of its TTL, and
            # inventing a QUEUED status for it would leave the UI polling
            # forever on a job nothing will ever run.
            raise ValueError(f"no job with id {job_id} (it may have expired)")
        return _decode(raw if isinstance(raw, str) else raw.decode())

    async def claim(self, *, timeout: int = 5) -> dict[str, str] | None:
        """Block until work arrives, or return None on timeout.

        BLPOP rather than a poll loop: the worker sleeps until there is
        something to do instead of hammering Redis.
        """
        result = await self._redis.blpop([_QUEUE], timeout=timeout)  # type: ignore[misc]
        if result is None:
            return None
        _, raw = result
        payload: dict[str, str] = json.loads(raw if isinstance(raw, str) else raw.decode())
        return payload

    async def mark_running(self, job_id: uuid.UUID, *, total: int = 0) -> None:
        await self._update(job_id, state=JobState.RUNNING, total=total)

    async def report_progress(self, job_id: uuid.UUID, *, processed: int, total: int) -> None:
        progress = (processed / total) if total else 0.0
        await self._update(job_id, processed=processed, total=total, progress=min(progress, 1.0))

    async def mark_complete(self, job_id: uuid.UUID, *, processed: int, total: int) -> None:
        await self._update(
            job_id,
            state=JobState.SUCCEEDED,
            processed=processed,
            total=total,
            progress=1.0,
            completed_at=datetime.now(UTC),
        )

    async def mark_aborted(self, job_id: uuid.UUID, reason: str) -> None:
        """A guard fired. Distinct from failure: the run was stopped on
        purpose, and the reason is kept verbatim so it can be read without
        re-running anything.
        """
        await self._update(
            job_id,
            state=JobState.ABORTED,
            aborted_reason=reason,
            completed_at=datetime.now(UTC),
        )

    async def mark_failed(self, job_id: uuid.UUID, error: Exception) -> None:
        await self._update(
            job_id,
            state=JobState.FAILED,
            errors=[JobError(code=type(error).__name__, message=str(error)[:500])],
            completed_at=datetime.now(UTC),
        )

    async def _update(self, job_id: uuid.UUID, **changes: Any) -> None:
        key = _KEY.format(job_id=job_id)
        raw = await self._redis.get(key)
        if raw is None:
            log.warning("job.update_missing", job_id=str(job_id))
            return
        current = _decode(raw if isinstance(raw, str) else raw.decode())
        updated = current.model_copy(update=changes)
        await self._redis.set(key, _encode(updated), ex=JOB_TTL_SECONDS)


def build_job_queue(redis_url: str) -> RedisJobQueue:
    client: redis.Redis = redis.from_url(redis_url, decode_responses=True)  # type: ignore[no-untyped-call]
    return RedisJobQueue(client)


__all__ = [
    "JOB_TTL_SECONDS",
    "JobQueue",
    "RedisJobQueue",
    "Stage",
    "build_job_queue",
]
