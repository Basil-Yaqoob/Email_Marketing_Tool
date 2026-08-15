"""robots.txt checking.

Not legally binding in most places, but ignoring it by default is how a
scraper gets IP-banned — we respect it with an explicit per-call override
(HttpClient.get(..., respect_robots=False)).

`urllib.robotparser` is synchronous and blocking, and its own fetch()
method would block the event loop — this module fetches the text with an
async HTTP call and hands the parser the text directly, never calling its
fetch() method.
"""

from __future__ import annotations

import urllib.robotparser
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.core.logging import get_logger

log = get_logger(__name__)

DEFAULT_TIMEOUT_SECONDS = 10.0


class RobotsChecker:
    def __init__(self, *, user_agent: str, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self._user_agent = user_agent
        self._timeout = timeout
        self._cache: dict[str, urllib.robotparser.RobotFileParser] = {}

    async def allowed(self, url: str) -> bool:
        parser = await self._get_parser(url)
        return parser.can_fetch(self._user_agent, url)

    async def _get_parser(self, url: str) -> urllib.robotparser.RobotFileParser:
        host_key, robots_url = self._robots_url(url)
        cached = self._cache.get(host_key)
        if cached is not None:
            return cached

        parser = urllib.robotparser.RobotFileParser()
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(robots_url, headers={"User-Agent": self._user_agent})
            if response.status_code == 200:
                parser.parse(response.text.splitlines())
            else:
                # Any non-200 (404, 403, 5xx, ...): fail open, no restrictions.
                parser.parse([])
        except httpx.HTTPError as exc:
            log.warning("robots.fetch_failed", url=robots_url, error=str(exc))
            parser.parse([])  # fail open, but logged — not silent

        self._cache[host_key] = parser
        return parser

    @staticmethod
    def _robots_url(url: str) -> tuple[str, str]:
        parts = urlsplit(url)
        host_key = f"{parts.scheme}://{parts.netloc}"
        robots_url = urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))
        return host_key, robots_url
