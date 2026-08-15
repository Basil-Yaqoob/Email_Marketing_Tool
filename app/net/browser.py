"""Playwright fallback for JavaScript-heavy pages.

Used only when a plain fetch returns essentially no text — an SPA shell
that needs a real browser to render. One shared browser instance per
BrowserFetcher: launching a fresh browser per page is very slow.

Playwright is imported lazily, inside start(), so importing this module
(or anything in app.net) never requires Playwright's browser binaries to
be installed unless a browser fetch actually happens.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from playwright.async_api import Browser

DEFAULT_TIMEOUT_MS = 15_000

_SCRIPT_TAG_RE = re.compile(r"<script\b", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")


def looks_like_spa(html: str, *, script_threshold: int = 5, text_threshold: int = 200) -> bool:
    """A high script-tag count paired with near-zero visible body text is
    the fingerprint of a JS-rendered shell a plain fetch cannot read. The
    crawler (Session 06) uses this to decide when to escalate to a real
    browser rather than escalating on every page.
    """
    script_count = len(_SCRIPT_TAG_RE.findall(html))
    visible_text = _TAG_RE.sub("", html).strip()
    return script_count >= script_threshold and len(visible_text) < text_threshold


class BrowserFetcher:
    def __init__(self) -> None:
        self._playwright: Any | None = None
        self._browser: Browser | None = None

    async def start(self) -> None:
        if self._browser is not None:
            return
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch()

    async def stop(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    async def fetch(
        self, url: str, *, wait_for: str | None = None, timeout_ms: float = DEFAULT_TIMEOUT_MS
    ) -> str:
        if self._browser is None:
            await self.start()
        assert self._browser is not None  # start() guarantees this

        page = await self._browser.new_page()
        try:
            await page.goto(url, timeout=timeout_ms)
            if wait_for is not None:
                await page.wait_for_selector(wait_for, timeout=timeout_ms)
            return await page.content()
        finally:
            await page.close()
