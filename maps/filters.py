"""Exact, deterministic filters applied after retrieval.

Search APIs offer coarse filters at best (Google Text Search: a minimum rating in
0.5 steps, no maximum, no review-count filter). Exact bounds are applied here, on
the retrieved set, so "rating no higher than 4.5 and at least 10 reviews" means
exactly that. All bounds are inclusive. A place whose rating or review count is
unknown fails any bound on that field: unknown is never assumed to qualify.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from .geography import BBox
from .models import Place


@dataclass
class Filters:
    min_rating: float | None = None
    max_rating: float | None = None
    min_reviews: int | None = None
    max_reviews: int | None = None
    types: list[str] = field(default_factory=list)  # keep places with any of these types
    exclude_types: list[str] = field(default_factory=list)
    open_only: bool = False  # drop permanently closed places
    name_regex: str | None = None
    bbox: BBox | None = None

    def active(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None and v is not False and v != []}


def _norm_type(t: str) -> str:
    return re.sub(r"[\s\-]+", "_", t.strip().casefold())


def _in_bounds(value: float | None, low: float | None, high: float | None) -> bool:
    if low is None and high is None:
        return True
    if value is None:
        return False
    return (low is None or value >= low) and (high is None or value <= high)


def keep(place: Place, f: Filters) -> bool:
    if not _in_bounds(place.rating, f.min_rating, f.max_rating):
        return False
    if not _in_bounds(place.review_count, f.min_reviews, f.max_reviews):
        return False
    all_types = {_norm_type(t) for t in place.types} | (
        {_norm_type(place.primary_type)} if place.primary_type else set()
    )
    if f.types and not all_types & {_norm_type(t) for t in f.types}:
        return False
    if f.exclude_types and all_types & {_norm_type(t) for t in f.exclude_types}:
        return False
    if f.open_only and place.business_status == "CLOSED_PERMANENTLY":
        return False
    if f.name_regex and not re.search(f.name_regex, place.name or "", re.IGNORECASE):
        return False
    if f.bbox and not f.bbox.contains(place.lat, place.lng):
        return False
    return True


def apply(places: Iterable[Place], f: Filters) -> list[Place]:
    return [p for p in places if keep(p, f)]
