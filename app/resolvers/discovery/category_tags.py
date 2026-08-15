"""Loader for the category -> OSM tag mapping.

Kept as a JSON data file (category_tags.json), not hardcoded in Python —
this list grows constantly as new business categories are added, and a
data file means adding one is a one-line PR, not a code change.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

_DATA_PATH: Final = Path(__file__).with_name("category_tags.json")


def load_category_tags(path: Path | None = None) -> Mapping[str, Sequence[Mapping[str, str]]]:
    target = path or _DATA_PATH
    data: dict[str, list[dict[str, str]]] = json.loads(target.read_text(encoding="utf-8"))
    return data
