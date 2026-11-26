"""Apify provider: Google Maps scraper actor `compass/crawler-google-places` (optional, paid).

Docs relied on: https://apify.com/compass/crawler-google-places/input-schema and
https://docs.apify.com/api/v2/act-run-sync-get-dataset-items-post (the API hosts and docs were NOT
reachable from the build machine; field names come from search snippets and examples, then mocks).

UNVERIFIED on a live run (check first):
- input `customGeolocation` as GeoJSON Polygon (coordinates are [lng, lat]); only Point is seen in the
  official examples. If Polygon is rejected, switch to Point + radiusKm.
- input `reviewsSort` value "newest", `maxReviews`, `placeIds`, `language`, `maxCrawledPlacesPerSearch`.
- output `publishedAtDate`, `reviewerUrl`, `reviewerNumberOfReviews`, `responseFromOwnerText`,
  `originalLanguage`, `reviewerId`, `location.lat/lng`, `categoryName`, `totalScore`, `reviewsCount`.
- sync endpoint returns HTTP 201 with a JSON list (200 is accepted too).
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from ..budget import Budget
from ..geography import BBox
from ..models import Hit, Place, Query, Review
from .base import ProviderError, SearchPage

ACTOR = "compass~crawler-google-places"
BASE_URL = "https://api.apify.com/v2"


class ApifyProvider:
    """Retrieval through the Apify Google Maps scraper (synchronous run, dataset items returned)."""

    name = "apify"

    def __init__(
        self,
        budget: Budget,
        *,
        token: str | None = None,
        client: httpx.Client | None = None,
        timeout: float = 300.0,
    ) -> None:
        self.budget = budget
        self._token = token if token is not None else os.environ.get("APIFY_TOKEN", "")
        self._client = client or httpx.Client(timeout=timeout)

    def available(self) -> bool:
        return bool(self._token)

    # -- HTTP ---------------------------------------------------------------------------------

    def _run(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        if not self._token:
            raise ProviderError("apify: APIFY_TOKEN is not set")
        url = f"{BASE_URL}/acts/{ACTOR}/run-sync-get-dataset-items"
        try:
            resp = self._client.post(
                url, json=payload, headers={"Authorization": f"Bearer {self._token}"}
            )
        except httpx.HTTPError as e:
            raise ProviderError(f"apify: request failed ({type(e).__name__})") from None
        if resp.status_code not in (200, 201):
            raise ProviderError(f"apify: HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError:
            raise ProviderError("apify: response is not JSON") from None
        if not isinstance(data, list):
            raise ProviderError("apify: unexpected payload (expected a list of items)")
        return [d for d in data if isinstance(d, dict)]

    # -- Provider protocol --------------------------------------------------------------------

    def text_search(
        self, query: Query, *, bbox: BBox | None, level: int = 0, max_results: int = 60
    ) -> SearchPage:
        self.budget.charge("apify.place", units=max_results)
        payload: dict[str, Any] = {
            "searchStringsArray": [query.text],
            "maxCrawledPlacesPerSearch": max_results,
            "maxReviews": 0,
        }
        if query.language:
            payload["language"] = query.language
        if bbox is not None:
            payload["customGeolocation"] = {
                "type": "Polygon",
                "coordinates": [[[lng, lat] for lat, lng in bbox.polygon()]],
            }
        items = self._run(payload)
        places: list[Place] = []
        for item in items:
            place_id = item.get("placeId")
            if not place_id:
                continue
            loc = item.get("location") or {}
            lat, lng = loc.get("lat"), loc.get("lng")
            if bbox is not None and not bbox.contains(lat, lng):
                continue
            cats = [c for c in (item.get("categories") or []) if isinstance(c, str)]
            place = Place(
                place_id=place_id,
                name=item.get("title") or "",
                lat=lat,
                lng=lng,
                rating=item.get("totalScore"),
                review_count=item.get("reviewsCount"),
                primary_type=item.get("categoryName"),
                types=cats,
                address=item.get("address"),
                website=item.get("website"),
                phone=item.get("phone"),
                maps_url=item.get("url"),
            )
            place.hits.append(
                Hit(
                    provider=self.name,
                    query=query.text,
                    language=query.language,
                    cell=bbox.key() if bbox else None,
                    level=level,
                    rank=len(places) + 1,
                )
            )
            places.append(place)
        return SearchPage(places=places, saturated=len(items) >= max_results)

    def reviews(self, place_id: str, *, limit: int = 5, language: str | None = None) -> list[Review]:
        self.budget.charge("apify.review", units=limit)
        payload: dict[str, Any] = {
            "placeIds": [place_id],
            "maxReviews": limit,
            "reviewsSort": "newest",
            "maxCrawledPlacesPerSearch": 1,
        }
        if language:
            payload["language"] = language
        out: list[Review] = []
        for item in self._run(payload):
            if item.get("placeId") not in (None, place_id):
                continue
            for r in item.get("reviews") or []:
                if isinstance(r, dict):
                    out.append(self._review(place_id, r))
        return out[:limit]

    def _review(self, place_id: str, r: dict[str, Any]) -> Review:
        return Review(
            place_id=place_id,
            text=r.get("text") or "",
            rating=r.get("stars"),
            language=r.get("originalLanguage"),
            published=r.get("publishedAtDate"),
            author=r.get("name"),
            author_id=r.get("reviewerUrl") or r.get("reviewerId"),
            author_review_count=r.get("reviewerNumberOfReviews"),
            owner_reply=r.get("responseFromOwnerText"),
            source=self.name,
        )
