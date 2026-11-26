"""Google Places API (New) provider: Text Search, Place Details, reviews and area resolution.

The field mask of each request selects the billed SKU, so the `tier` decides cost:
ids (free, ids only) < pro < enterprise. Auth is by the `X-Goog-Api-Key` header only; the key is never
placed in a URL, an error message or a log line.

Every page request is charged to the budget immediately before it is sent, because each page of a
Text Search is billed separately.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from typing import Any

import httpx

from ..budget import Budget, BudgetExceeded
from ..geography import BBox
from ..models import Hit, Place, Query, Review
from .base import ProviderError, QuotaExceeded, SearchPage

BASE_URL = "https://places.googleapis.com/v1"
SEARCH_URL = f"{BASE_URL}/places:searchText"
PAGE_SIZE = 20
RESULT_CAP = 60  # Google returns at most 3 pages of 20 per query

_IDS = ["places.id"]
_PRO = _IDS + [
    "places.displayName",
    "places.formattedAddress",
    "places.location",
    "places.types",
    "places.primaryType",
    "places.businessStatus",
    "places.googleMapsUri",
]
_ENTERPRISE = _PRO + [
    "places.rating",
    "places.userRatingCount",
    "places.priceLevel",
    "places.websiteUri",
    "places.nationalPhoneNumber",
]
TIER_FIELDS: dict[str, list[str]] = {"ids": _IDS, "pro": _PRO, "enterprise": _ENTERPRISE}


def _check_tier(tier: str) -> str:
    if tier not in TIER_FIELDS:
        raise ValueError(f"unknown tier {tier!r}; expected one of {sorted(TIER_FIELDS)}")
    return tier


def search_mask(tier: str) -> str:
    return ",".join(["nextPageToken", *TIER_FIELDS[_check_tier(tier)]])


def details_mask(tier: str) -> str:
    return ",".join(f.removeprefix("places.") for f in TIER_FIELDS[_check_tier(tier)])


class GooglePlaces:
    """Retrieval through the official Places API (New)."""

    name = "google"

    def __init__(
        self,
        budget: Budget,
        *,
        api_key: str | None = None,
        client: httpx.Client | None = None,
        tier: str = "enterprise",
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.budget = budget
        self.tier = _check_tier(tier)
        self._key = api_key if api_key is not None else os.environ.get("GOOGLE_MAPS_API_KEY", "")
        self._client = client or httpx.Client(timeout=30.0)
        self._sleep = sleep

    def available(self) -> bool:
        return bool(self._key)

    # -- HTTP ---------------------------------------------------------------------------------

    def _scrub(self, text: str) -> str:
        """Google sometimes echoes the key in error text; never let it reach a message."""
        return text.replace(self._key, "***") if self._key else text

    def _call(
        self,
        method: str,
        url: str,
        *,
        sku: str,
        mask: str,
        json: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Charge once, send, retry once on 429/503. Raises BudgetExceeded before any HTTP."""
        if not self._key:
            raise ProviderError("google: GOOGLE_MAPS_API_KEY is not set")
        self.budget.charge(sku)
        headers = {"X-Goog-Api-Key": self._key, "X-Goog-FieldMask": mask}
        resp: httpx.Response | None = None
        for attempt in (0, 1):
            try:
                resp = self._client.request(method, url, json=json, params=params, headers=headers)
            except httpx.HTTPError as e:
                raise ProviderError(f"google: request failed ({type(e).__name__})") from None
            if resp.status_code in (429, 503) and attempt == 0:
                self._sleep(2)
                continue
            break
        assert resp is not None
        if resp.status_code == 429:
            raise QuotaExceeded(
                "google: HTTP 429, the provider's quota was reached; "
                "`maps search --resume RUN` continues later"
            )
        if not 200 <= resp.status_code < 300:
            detail = ""
            try:
                err = resp.json().get("error")
                if isinstance(err, dict):
                    detail = str(err.get("message") or "")
            except (ValueError, AttributeError):
                pass
            msg = f"google: HTTP {resp.status_code}" + (f": {detail}" if detail else "")
            raise ProviderError(self._scrub(msg))
        try:
            data = resp.json() if resp.content else {}
        except ValueError:
            raise ProviderError("google: response is not JSON") from None
        if not isinstance(data, dict):
            raise ProviderError("google: unexpected payload (expected an object)")
        return data

    # -- Provider protocol --------------------------------------------------------------------

    def text_search(
        self,
        query: Query,
        *,
        bbox: BBox | None,
        level: int = 0,
        max_results: int = RESULT_CAP,
        min_rating: float | None = None,
        included_type: str | None = None,
        tier: str | None = None,
    ) -> SearchPage:
        tier = _check_tier(tier or self.tier)
        cap = min(max_results, RESULT_CAP)
        base: dict[str, Any] = {"textQuery": query.text}
        if query.language:
            base["languageCode"] = query.language
        if bbox is not None:
            base["locationRestriction"] = {
                "rectangle": {
                    "low": {"latitude": bbox.south, "longitude": bbox.west},
                    "high": {"latitude": bbox.north, "longitude": bbox.east},
                }
            }
        if min_rating is not None:
            base["minRating"] = min_rating
        if included_type:
            base["includedType"] = included_type
            base["strictTypeFiltering"] = True

        sku, mask = f"google.text_search.{tier}", search_mask(tier)
        places: list[Place] = []
        token: str | None = None
        stopped_early: str | None = None
        budget_exhausted = False
        while len(places) < cap:
            body = {**base, "pageSize": min(PAGE_SIZE, cap - len(places))}
            if token:
                body["pageToken"] = token
            try:
                data = self._call("POST", SEARCH_URL, sku=sku, mask=mask, json=body)
            except (BudgetExceeded, QuotaExceeded) as e:
                if not places:
                    raise
                stopped_early, budget_exhausted = str(e), True
                break
            except ProviderError as e:
                if not places:
                    raise
                stopped_early = str(e)
                break
            for raw in data.get("places") or []:
                if isinstance(raw, dict) and raw.get("id"):
                    place = self._place(raw)
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
            token = data.get("nextPageToken") or None
            if not token:
                break
        places = places[:cap]
        saturated = stopped_early is not None or (
            len(places) >= cap and (bool(token) or len(places) == RESULT_CAP)
        )
        return SearchPage(
            places=places, saturated=saturated, stopped_early=stopped_early, budget_exhausted=budget_exhausted
        )

    def details(self, place_id: str, *, tier: str = "enterprise", language: str | None = None) -> Place:
        tier = _check_tier(tier)
        data = self._call(
            "GET",
            f"{BASE_URL}/places/{place_id}",
            sku=f"google.details.{tier}",
            mask=details_mask(tier),
            params={"languageCode": language} if language else None,
        )
        data.setdefault("id", place_id)
        return self._place(data)

    def reviews(self, place_id: str, *, limit: int = 5, language: str | None = None) -> list[Review]:
        """Google returns at most 5 reviews and offers no sort parameter."""
        data = self._call(
            "GET",
            f"{BASE_URL}/places/{place_id}",
            sku="google.details.atmosphere",
            mask="id,reviews",
            params={"languageCode": language} if language else None,
        )
        out = [self._review(place_id, r) for r in data.get("reviews") or [] if isinstance(r, dict)]
        return out[:limit]

    def resolve_area(self, name: str) -> tuple[BBox, str]:
        """Turn a place name into its viewport box, for use as a search area."""
        data = self._call(
            "POST",
            SEARCH_URL,
            sku="google.text_search.pro",
            mask="places.id,places.displayName,places.formattedAddress,places.location,places.viewport",
            json={"textQuery": name, "pageSize": 1},
        )
        found = data.get("places") or []
        if not found:
            raise ProviderError(f"google: no place found for area {name!r}")
        top = found[0]
        vp = top.get("viewport") or {}
        try:
            low, high = vp["low"], vp["high"]
            bbox = BBox(low["latitude"], low["longitude"], high["latitude"], high["longitude"])
        except (KeyError, TypeError, ValueError):
            raise ProviderError(f"google: place for area {name!r} has no usable viewport") from None
        title = (top.get("displayName") or {}).get("text") or name
        addr = top.get("formattedAddress")
        return bbox, f"{title}, {addr}" if addr else title

    # -- mapping ------------------------------------------------------------------------------

    def _place(self, d: dict[str, Any]) -> Place:
        loc = d.get("location") or {}
        return Place(
            place_id=d["id"],
            name=(d.get("displayName") or {}).get("text") or "",
            lat=loc.get("latitude"),
            lng=loc.get("longitude"),
            rating=d.get("rating"),
            review_count=d.get("userRatingCount"),
            primary_type=d.get("primaryType"),
            types=[t for t in d.get("types") or [] if isinstance(t, str)],
            address=d.get("formattedAddress"),
            price_level=d.get("priceLevel"),
            business_status=d.get("businessStatus"),
            website=d.get("websiteUri"),
            phone=d.get("nationalPhoneNumber"),
            maps_url=d.get("googleMapsUri"),
        )

    def _review(self, place_id: str, r: dict[str, Any]) -> Review:
        # Prefer the author's own words over Google's translation into the request language.
        body = r.get("originalText") or r.get("text") or {}
        author = r.get("authorAttribution") or {}
        return Review(
            place_id=place_id,
            text=body.get("text") or "",
            rating=r.get("rating"),
            language=body.get("languageCode"),
            published=r.get("publishTime"),
            author=author.get("displayName"),
            author_id=author.get("uri"),
            source=self.name,
            url=r.get("googleMapsUri"),
            flag_url=r.get("flagContentUri"),
        )
