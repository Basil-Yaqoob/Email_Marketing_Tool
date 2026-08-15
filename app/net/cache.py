"""Content-addressed on-disk HTTP response cache.

Keyed by sha256(method + url), storing the raw response bytes — not
decoded text — so a parser fix can be re-run against the exact original
response without a re-fetch. Re-crawling 1,700 sites because a parser had
a bug is slow and rude; this is the fix.

CLAUDE.md rule 6: all I/O is async. The actual filesystem calls are
blocking, so they run in a worker thread via asyncio.to_thread rather than
blocking the event loop.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TTL_SECONDS = 7 * 24 * 3600  # 7 days


@dataclass(frozen=True, slots=True)
class CachedResponse:
    status_code: int
    headers: dict[str, str]
    body: bytes
    fetched_at: float  # unix timestamp


class ResponseCache:
    def __init__(
        self,
        root: Path | str,
        *,
        default_ttl_seconds: float = DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._default_ttl = default_ttl_seconds
        # Injectable so TTL expiry can be tested by advancing a fake clock
        # instead of sleeping — doc/03-TESTING.md: no sleep() in tests.
        self._clock = clock

    async def get(
        self, method: str, url: str, *, ttl_seconds: float | None = None
    ) -> CachedResponse | None:
        return await asyncio.to_thread(self._get_sync, method, url, ttl_seconds)

    async def put(
        self, method: str, url: str, *, status_code: int, headers: Mapping[str, str], body: bytes
    ) -> None:
        await asyncio.to_thread(self._put_sync, method, url, status_code, dict(headers), body)

    def _key(self, method: str, url: str) -> str:
        return hashlib.sha256(f"{method.upper()} {url}".encode()).hexdigest()

    def _paths(self, key: str) -> tuple[Path, Path]:
        return self._root / f"{key}.json", self._root / f"{key}.body"

    def _get_sync(self, method: str, url: str, ttl_seconds: float | None) -> CachedResponse | None:
        key = self._key(method, url)
        meta_path, body_path = self._paths(key)
        if not meta_path.exists() or not body_path.exists():
            return None

        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        ttl = self._default_ttl if ttl_seconds is None else ttl_seconds
        if self._clock() - meta["fetched_at"] > ttl:
            return None

        return CachedResponse(
            status_code=meta["status_code"],
            headers=meta["headers"],
            body=body_path.read_bytes(),
            fetched_at=meta["fetched_at"],
        )

    def _put_sync(
        self, method: str, url: str, status_code: int, headers: dict[str, str], body: bytes
    ) -> None:
        key = self._key(method, url)
        meta_path, body_path = self._paths(key)
        meta = {"status_code": status_code, "headers": headers, "fetched_at": self._clock()}
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        body_path.write_bytes(body)
