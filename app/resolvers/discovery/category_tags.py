"""Loader for the category -> OSM tag mapping.

Kept as a JSON data file (category_tags.json), not hardcoded in Python —
this list grows constantly as new business categories are added, and a
data file means adding one is a one-line PR, not a code change.

Lookup is normalised (case-folded, whitespace-collapsed) because the
category comes from a text box a person typed into: "Dental Clinic",
"dental  clinic" and "dental clinic" are the same request, and treating
them as different is the sort of thing that returns zero results with no
explanation.
"""

from __future__ import annotations

import difflib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

_DATA_PATH: Final = Path(__file__).with_name("category_tags.json")

# Keys beginning with "_" are documentation inside the data file, not
# categories.
_METADATA_PREFIX: Final = "_"


def normalise(category: str) -> str:
    return " ".join(category.lower().split())


def load_category_tags(path: Path | None = None) -> Mapping[str, Sequence[Mapping[str, str]]]:
    target = path or _DATA_PATH
    data: dict[str, object] = json.loads(target.read_text(encoding="utf-8"))
    return {
        normalise(key): value  # type: ignore[misc]
        for key, value in data.items()
        if not key.startswith(_METADATA_PREFIX)
    }


class UnknownCategoryError(ValueError):
    """Raised when a category has no OSM tag mapping.

    This is deliberately loud. An unmapped category contributes no clauses
    to the Overpass query, so the previous behaviour -- skip it silently --
    produced an empty query, zero results, and a successful-looking run
    (CLAUDE.md §2.1). Someone searching for "dentist" would be told
    nothing at all rather than that the word was not recognised.
    """

    def __init__(self, category: str, known: Sequence[str]) -> None:
        suggestions = difflib.get_close_matches(normalise(category), known, n=3, cutoff=0.6)
        hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
        super().__init__(
            f"unknown business category {category!r} — it has no OpenStreetMap tag "
            f"mapping, so searching for it would return nothing.{hint} "
            f"{len(known)} categories are supported; add more in "
            f"app/resolvers/discovery/category_tags.json."
        )
        self.category = category
        self.suggestions = tuple(suggestions)


def tags_for(
    category: str, tags: Mapping[str, Sequence[Mapping[str, str]]]
) -> Sequence[Mapping[str, str]]:
    """Tag sets for one category, raising if it is not mapped."""
    resolved = tags.get(normalise(category))
    if resolved is None:
        raise UnknownCategoryError(category, sorted(tags))
    return resolved


__all__ = [
    "UnknownCategoryError",
    "load_category_tags",
    "normalise",
    "tags_for",
]
