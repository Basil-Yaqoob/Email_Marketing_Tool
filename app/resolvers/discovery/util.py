"""Shared helpers for turning a raw website string into a usable URL/domain.

OSM `website` tags are frequently missing the scheme ("example.com" not
"https://example.com") — normalise before storing, or every downstream
consumer has to guess.
"""

from __future__ import annotations

from urllib.parse import urlsplit


def ensure_scheme(url: str) -> str:
    if url.startswith(("http://", "https://")):
        return url
    return f"https://{url}"


def normalise_domain(url: str) -> str:
    netloc = urlsplit(ensure_scheme(url)).netloc
    return netloc.lower().removeprefix("www.")
