"""Google Places Aggregate API (areainsights `computeInsights`): counts and lists places by type.

Why it exists: Text Search has no maximum-rating filter and caps results at 60, so "all bakeries
rated 3.5 or lower in this city" cannot be asked of it. Aggregate filters by type and by exact
rating bounds, counts without paging, and lists up to 100 place IDs per request.

UNVERIFIED on a live call: the request/response field names (`customArea.polygon.coordinates`,
`typeFilter.includedTypes`, `placeInsights[].place`) and the 100-place limit for INSIGHT_PLACES were
taken from Google's documentation via search excerpts on 2026-11-26 and are to be confirmed on the
first live call.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..budget import Budget, BudgetExceeded
from ..geography import BBox
from .base import ProviderError

URL = "https://areainsights.googleapis.com/v1:computeInsights"
PLACES_LIMIT = 100  # INSIGHT_PLACES returns at most this many IDs per request


@dataclass
class EnumerationResult:
    place_ids: list[str] = field(default_factory=list)  # deduplicated, first-seen order
    total_count: int = 0  # count for the whole area (top-level request)
    requests: int = 0  # HTTP requests actually sent
    unresolved: list[str] = field(default_factory=list)  # BBox keys of cells not fully listed
    stopped: str | None = None  # budget message if enumeration ended early


class PlacesAggregate:
    """Count and enumerate places by type with exact rating bounds."""

    name = "google_aggregate"

    def __init__(
        self, budget: Budget, *, api_key: str | None = None, client: httpx.Client | None = None
    ) -> None:
        self.budget = budget
        self._key = api_key if api_key is not None else os.environ.get("GOOGLE_MAPS_API_KEY", "")
        self._client = client or httpx.Client(timeout=60.0)

    def available(self) -> bool:
        return bool(self._key)

    # -- HTTP ---------------------------------------------------------------------------------

    def _compute(
        self,
        insight: str,
        bbox: BBox,
        types: list[str],
        min_rating: float | None,
        max_rating: float | None,
        operational_only: bool,
    ) -> dict[str, Any]:
        if not self._key:
            raise ProviderError("google aggregate: GOOGLE_MAPS_API_KEY is not set")
        flt: dict[str, Any] = {
            "locationFilter": {
                "customArea": {
                    "polygon": {
                        "coordinates": [{"latitude": lat, "longitude": lng} for lat, lng in bbox.polygon()]
                    }
                }
            },
            "typeFilter": {"includedTypes": types},
        }
        if operational_only:
            flt["operatingStatus"] = ["OPERATING_STATUS_OPERATIONAL"]
        rating = {
            k: v for k, v in (("minRating", min_rating), ("maxRating", max_rating)) if v is not None
        }
        if rating:
            flt["ratingFilter"] = rating
        self.budget.charge("google.aggregate")
        try:
            resp = self._client.post(
                URL,
                json={"insights": [insight], "filter": flt},
                headers={"X-Goog-Api-Key": self._key},
            )
        except httpx.HTTPError as e:
            raise ProviderError(f"google aggregate: request failed ({type(e).__name__})") from None
        if not 200 <= resp.status_code < 300:
            detail = ""
            try:
                err = resp.json().get("error")
                if isinstance(err, dict):
                    detail = str(err.get("message") or "")
            except (ValueError, AttributeError):
                pass
            msg = f"google aggregate: HTTP {resp.status_code}" + (f": {detail}" if detail else "")
            raise ProviderError(msg.replace(self._key, "***"))
        try:
            data = resp.json() if resp.content else {}
        except ValueError:
            raise ProviderError("google aggregate: response is not JSON") from None
        if not isinstance(data, dict):
            raise ProviderError("google aggregate: unexpected payload (expected an object)")
        return data

    # -- public API ---------------------------------------------------------------------------

    def count(
        self,
        bbox: BBox,
        *,
        types: list[str],
        min_rating: float | None = None,
        max_rating: float | None = None,
        operational_only: bool = True,
    ) -> int:
        data = self._compute("INSIGHT_COUNT", bbox, types, min_rating, max_rating, operational_only)
        try:
            return int(data.get("count") or 0)  # Google serialises int64 as a string
        except (TypeError, ValueError):
            raise ProviderError("google aggregate: unparseable count") from None

    def place_ids(
        self,
        bbox: BBox,
        *,
        types: list[str],
        min_rating: float | None = None,
        max_rating: float | None = None,
        operational_only: bool = True,
    ) -> list[str]:
        data = self._compute("INSIGHT_PLACES", bbox, types, min_rating, max_rating, operational_only)
        out: list[str] = []
        for item in data.get("placeInsights") or []:
            place = item.get("place") if isinstance(item, dict) else None
            if isinstance(place, str) and place:
                out.append(place.removeprefix("places/"))
        return out

    def enumerate(
        self,
        bbox: BBox,
        *,
        types: list[str],
        min_rating: float | None = None,
        max_rating: float | None = None,
        operational_only: bool = True,
        max_level: int = 8,
    ) -> EnumerationResult:
        """Quadtree walk: count a cell, list it if it fits under the limit, otherwise split it.

        Stops cleanly on BudgetExceeded and reports the cells it did not get to.
        """
        kw: dict[str, Any] = dict(
            types=types, min_rating=min_rating, max_rating=max_rating, operational_only=operational_only
        )
        res = EnumerationResult()
        seen: set[str] = set()
        stack: list[tuple[BBox, int]] = [(bbox, 0)]
        while stack:
            cell, level = stack.pop()
            try:
                n = self.count(cell, **kw)
                res.requests += 1
                if level == 0:
                    res.total_count = n
                if n == 0:
                    continue
                if n <= PLACES_LIMIT:
                    ids = self.place_ids(cell, **kw)
                    res.requests += 1
                    for pid in ids:
                        if pid not in seen:
                            seen.add(pid)
                            res.place_ids.append(pid)
                elif level >= max_level:
                    res.unresolved.append(cell.key())
                else:
                    stack.extend((c, level + 1) for c in reversed(cell.split()))
            except BudgetExceeded as e:
                res.stopped = str(e)
                res.unresolved.append(cell.key())
                res.unresolved.extend(c.key() for c, _ in reversed(stack))
                break
        return res
