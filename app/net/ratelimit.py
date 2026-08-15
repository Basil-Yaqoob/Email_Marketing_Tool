"""Per-host async token bucket rate limiting.

Defaults: 1 req/sec per host, burst of 3, global concurrency cap of 20.
All configurable — some registries permit far more, some small business
sites deserve far less than the default.

Must be safe under concurrency: 50 coroutines hitting one host must never
exceed the configured rate. Each host gets its own bucket guarded by its
own asyncio.Lock, so token math for one host never blocks another.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

DEFAULT_REQUESTS_PER_SECOND = 1.0
DEFAULT_BURST = 3
DEFAULT_GLOBAL_CONCURRENCY = 20


@dataclass(slots=True)
class _Bucket:
    capacity: float
    rate_per_sec: float
    tokens: float
    updated_at: float
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class RateLimiter:
    def __init__(
        self,
        *,
        requests_per_second: float = DEFAULT_REQUESTS_PER_SECOND,
        burst: int = DEFAULT_BURST,
        global_concurrency: int = DEFAULT_GLOBAL_CONCURRENCY,
    ) -> None:
        self._rate = requests_per_second
        self._burst = burst
        self._buckets: dict[str, _Bucket] = {}
        # Guards creation of new per-host buckets only — not held during
        # token consumption, so different hosts never wait on each other.
        self._buckets_lock = asyncio.Lock()
        self._global_semaphore = asyncio.Semaphore(global_concurrency)

    async def _get_bucket(self, host: str) -> _Bucket:
        async with self._buckets_lock:
            bucket = self._buckets.get(host)
            if bucket is None:
                bucket = _Bucket(
                    capacity=float(self._burst),
                    rate_per_sec=self._rate,
                    tokens=float(self._burst),
                    updated_at=time.monotonic(),
                )
                self._buckets[host] = bucket
            return bucket

    @asynccontextmanager
    async def acquire(self, host: str) -> AsyncIterator[None]:
        """Hold a global concurrency slot and a per-host rate-limit token
        for the duration of the `async with` block — typically one HTTP
        request. Usage: `async with limiter.acquire(host): ...`.
        """
        async with self._global_semaphore:
            bucket = await self._get_bucket(host)
            async with bucket.lock:
                await self._consume_token(bucket)
            yield

    async def _consume_token(self, bucket: _Bucket) -> None:
        while True:
            now = time.monotonic()
            elapsed = now - bucket.updated_at
            bucket.tokens = min(bucket.capacity, bucket.tokens + elapsed * bucket.rate_per_sec)
            bucket.updated_at = now

            if bucket.tokens >= 1:
                bucket.tokens -= 1
                return

            deficit = 1 - bucket.tokens
            wait_seconds = deficit / bucket.rate_per_sec
            await asyncio.sleep(wait_seconds)
