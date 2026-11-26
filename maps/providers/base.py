"""The contract every retrieval provider implements.

A provider turns (keyword, area, language) into `Place` objects and, optionally,
fetches reviews. Providers charge the shared `Budget` *before* each billable
request, so a search stops before it spends rather than after.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from ..budget import Budget
from ..geography import BBox
from ..models import Place, Query, Review


class ProviderError(RuntimeError):
    """A provider request failed (HTTP error, bad payload, missing credentials)."""


@dataclass
class SearchPage:
    places: list[Place] = field(default_factory=list)
    # True when the provider hit its own result cap for this query+area, i.e.
    # more matching places probably exist and a smaller area would reveal them.
    saturated: bool = False
    # Why the provider returned fewer results than it could have (budget refusal, page error).
    stopped_early: str | None = None
    budget_exhausted: bool = False


@runtime_checkable
class Provider(Protocol):
    name: str
    budget: Budget

    def available(self) -> bool:
        """True when credentials for this provider are configured."""
        ...

    def text_search(
        self, query: Query, *, bbox: BBox | None, level: int = 0, max_results: int = 60
    ) -> SearchPage:
        """Keyword search restricted to bbox. Must tag each Place with a Hit."""
        ...

    def reviews(self, place_id: str, *, limit: int = 5, language: str | None = None) -> list[Review]:
        """Reviews for one place, newest or most relevant first as the provider allows."""
        ...
