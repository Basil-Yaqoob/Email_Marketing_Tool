"""Model pricing, and honesty about how old it is.

Model prices change and models get retired. A cost figure computed from a
year-old table is worse than no figure, because it looks authoritative.
So the table carries a `fetched_at`, `is_stale` is a first-class property,
and both staleness and unknown-model lookups surface as warnings on the
Result the caller gets back — not just a log line nobody reads.

An unknown model costs Decimal("0") *with a warning*, deliberately, rather
than raising: an un-priced model should not break a campaign, but it must
never be silently reported as free.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from app.llm.types import Usage

_PRICES_FILE = Path(__file__).with_name("model_prices.json")

MAX_PRICE_AGE_DAYS = 90
TOKENS_PER_PRICE_UNIT = Decimal("1000000")


@dataclass(frozen=True, slots=True)
class ModelPrice:
    prompt_per_million: Decimal
    completion_per_million: Decimal


class PriceTable:
    def __init__(
        self,
        prices: dict[str, ModelPrice],
        *,
        fetched_at: date,
        max_age_days: int = MAX_PRICE_AGE_DAYS,
    ) -> None:
        self._prices = prices
        self._fetched_at = fetched_at
        self._max_age_days = max_age_days

    @classmethod
    def load(
        cls, path: Path | None = None, *, max_age_days: int = MAX_PRICE_AGE_DAYS
    ) -> PriceTable:
        data = json.loads((path or _PRICES_FILE).read_text(encoding="utf-8"))
        prices = {
            model: ModelPrice(
                prompt_per_million=Decimal(str(entry["prompt"])),
                completion_per_million=Decimal(str(entry["completion"])),
            )
            for model, entry in data["models"].items()
        }
        return cls(
            prices,
            fetched_at=date.fromisoformat(data["fetched_at"]),
            max_age_days=max_age_days,
        )

    @property
    def fetched_at(self) -> date:
        return self._fetched_at

    def age_days(self, *, today: date | None = None) -> int:
        return ((today or datetime.now(UTC).date()) - self._fetched_at).days

    def is_stale(self, *, today: date | None = None) -> bool:
        return self.age_days(today=today) > self._max_age_days

    def knows(self, model: str) -> bool:
        return model in self._prices

    def cost(self, model: str, usage: Usage) -> tuple[Decimal, tuple[str, ...]]:
        """(cost, warnings). Warnings are returned rather than logged-and-
        forgotten so they can reach the caller on the Result.
        """
        warnings: list[str] = []
        if self.is_stale():
            warnings.append(
                f"model price data is {self.age_days()} days old "
                f"(fetched {self._fetched_at.isoformat()}); costs may be wrong"
            )

        price = self._prices.get(model)
        if price is None:
            warnings.append(f"no price data for model {model!r}; cost recorded as 0")
            return Decimal("0"), tuple(warnings)

        cost = (
            Decimal(usage.prompt_tokens) * price.prompt_per_million
            + Decimal(usage.completion_tokens) * price.completion_per_million
        ) / TOKENS_PER_PRICE_UNIT
        return cost, tuple(warnings)
