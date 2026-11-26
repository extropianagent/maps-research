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

Contributor-first discovery (`contributor_reviews`): given a reviewer, list the OTHER places they
reviewed. Google's official APIs cannot do this. Everything below comes from search excerpts of
2026-11-26 and is UNCONFIRMED (the Apify site was blocked from the build machine):
- actor `lexis-solutions~google-maps-contributor-reviews-scraper` (the only one), input `startUrls`
  (contributor URLs like https://www.google.com/maps/contrib/<id>/reviews) and `maxItems`.
- output fields `placeId`, `cid`, `rating`, `reviewCount`, `updatedAt`, `relativeDate`; the names of
  the contributor's own star rating, review text and place name are NOT confirmed, so parsing tries
  several candidates (`stars`/`reviewRating`/..., `text`/`reviewText`/..., `title`/`placeName`/...).
  A bare `rating` is only treated as the review's rating when no place-rating field exists.
- the actor may return only about the 10 most recent reviews, whatever `limit` is.
Fixed and reviewed: the actor ids are constants in this module (`ACTOR`, `CONTRIBUTOR_ACTORS`) and
every input is built in code. Nothing but `APIFY_TOKEN` is read from the environment, so no variable
can point this module at another actor or reshape what is sent. Adding an actor means reviewing it
and editing the allow-list here.

Privacy: this retrieves individual reviewers' public review histories. It is off unless APIFY_TOKEN
is set, and should be used only on contributors surfaced by the owner's own searches.
"""

from __future__ import annotations

import os
import re
from typing import Any

import httpx

from .. import policy
from ..budget import Budget
from ..geography import BBox
from ..models import Hit, Place, Query, Review
from .base import PolicyStop, ProviderError, QuotaExceeded, SearchPage
from .http import make_client

ACTOR = "compass~crawler-google-places"
BASE_URL = "https://api.apify.com/v2"
CONTRIBUTOR_ACTORS = ("lexis-solutions~google-maps-contributor-reviews-scraper",)  # reviewed allow-list
CONTRIBUTOR_ACTOR = CONTRIBUTOR_ACTORS[0]
ALLOWED_ACTORS = (ACTOR, *CONTRIBUTOR_ACTORS)
CONTRIBUTOR_SOURCE = "apify_contributor"
_CONTRIB_URL_RE = re.compile(r"/contrib/(\d{10,})(?!\d)")
_CONTRIB_ID_RE = re.compile(r"\d{10,}")

_PLACE_ID_KEYS = ("placeId", "place_id", "placeID")
_NAME_KEYS = ("title", "placeName", "name", "placeTitle")
_ADDRESS_KEYS = ("address", "placeAddress")
_PLACE_RATING_KEYS = ("totalScore", "placeRating")
_REVIEW_RATING_KEYS = ("stars", "reviewRating", "reviewStars", "userRating")
_REVIEW_COUNT_KEYS = ("reviewCount", "reviewsCount")
_URL_KEYS = ("url", "placeUrl", "googleMapsUrl")
_TEXT_KEYS = ("text", "reviewText", "review", "snippet")
_PUBLISHED_KEYS = ("publishedAtDate", "updatedAt", "date")


def parse_contributor(value: str) -> tuple[str, str]:
    """Normalise a contributor URL or bare numeric id to (id, canonical reviews URL)."""
    v = (value or "").strip()
    m = _CONTRIB_URL_RE.search(v) or (_CONTRIB_ID_RE.fullmatch(v) if v.isdigit() else None)
    if not m:
        raise ValueError("expected a Google Maps contributor URL or a numeric id of at least 10 digits")
    cid = m.group(1) if m.re is _CONTRIB_URL_RE else m.group(0)
    return cid, f"https://www.google.com/maps/contrib/{cid}/reviews"


def _first(item: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for k in keys:
        v = item.get(k)
        if v is not None and v != "":
            return v
    return None


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
        self._client = client or make_client(timeout=timeout)

    def available(self) -> bool:
        return bool(self._token)

    # -- HTTP ---------------------------------------------------------------------------------

    @staticmethod
    def _url(actor: str) -> str:
        return f"{BASE_URL}/acts/{actor}/run-sync-get-dataset-items"

    def _run(self, payload: dict[str, Any], actor: str = ACTOR) -> list[dict[str, Any]]:
        if actor not in ALLOWED_ACTORS:
            raise ProviderError(f"apify: actor {actor!r} is not in the reviewed allow-list")
        if not self._token:
            raise ProviderError("apify: APIFY_TOKEN is not set")
        policy.check_outbound_payload(self.name, payload)
        url = self._url(actor)
        try:
            resp = self._client.post(url, json=payload, headers={"Authorization": f"Bearer {self._token}"})
        except httpx.HTTPError as e:
            raise ProviderError(f"apify: request failed ({type(e).__name__})") from None
        if not 200 <= resp.status_code < 300:
            reason = policy.detect_enforcement(resp.status_code, policy.enforcement_text(resp.text))
            if reason:
                raise PolicyStop(self.name, f"HTTP {resp.status_code}: {reason}")
        if resp.status_code == 429:
            raise QuotaExceeded("apify: HTTP 429 (rate or quota limit)")
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
        self, query: Query, *, bbox: BBox | None, level: int = 0, max_results: int = 60, **_: Any
    ) -> SearchPage:
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
        self.budget.charge(
            "apify.place",
            units=max_results,
            fingerprint=Budget.fingerprint("POST", self._url(ACTOR), payload),
        )
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
        """Reviews of one place. Reserves `limit` x `apify.review` AND 1 x `apify.place` in one charge:
        the actor scrapes the place itself, so the place is billed too. The actor's exact billing events
        for a reviews run are unconfirmed (not checked against a live bill); the reservation errs on the
        side of counting more."""
        payload: dict[str, Any] = {
            "placeIds": [place_id],
            "maxReviews": limit,
            "reviewsSort": "newest",
            "maxCrawledPlacesPerSearch": 1,
        }
        if language:
            payload["language"] = language
        self.budget.charge_many(
            [("apify.place", 1), ("apify.review", limit)],
            fingerprint=Budget.fingerprint("POST", self._url(ACTOR), payload),
        )
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

    # -- Contributor-first discovery (field names unconfirmed, see module docstring) ------------

    def contributor_reviews(self, contributor: str, *, limit: int = 10) -> list[Place]:
        """Places this contributor reviewed, each carrying their one review (source apify_contributor)."""
        cid, curl = parse_contributor(contributor)
        if limit < 1:
            return []
        payload = {"startUrls": [{"url": curl}], "maxItems": limit}
        policy.check_outbound_payload(self.name, payload)
        self.budget.charge(
            "apify.contributor",
            units=limit,
            fingerprint=Budget.fingerprint("POST", self._url(CONTRIBUTOR_ACTOR), payload),
        )
        places: list[Place] = []
        seen: set[str] = set()
        for item in self._run(payload, CONTRIBUTOR_ACTOR):
            place = self._contributor_place(item, curl)
            if place is None or place.place_id in seen:
                continue
            seen.add(place.place_id)
            place.hits.append(
                Hit(provider=CONTRIBUTOR_SOURCE, query=f"contributor:{cid}", rank=len(places) + 1)
            )
            places.append(place)
            if len(places) >= limit:
                break
        return places

    @staticmethod
    def _contributor_place(item: dict[str, Any], curl: str) -> Place | None:
        pid = _first(item, _PLACE_ID_KEYS)
        if not isinstance(pid, str) or len(pid) < 10:
            return None
        review_rating = _first(item, _REVIEW_RATING_KEYS)
        place_rating = _first(item, _PLACE_RATING_KEYS)
        plain = item.get("rating")
        if review_rating is not None:
            place_rating = place_rating if place_rating is not None else plain
        elif place_rating is None:
            review_rating = plain  # only one rating present: it is the reviewer's
        name = _first(item, _NAME_KEYS)
        author = item.get("reviewerName")
        other_name = _first(item, ("title", "placeName", "placeTitle"))
        if not author and other_name and isinstance(item.get("name"), str) and item["name"] != other_name:
            author = item["name"]
        loc = item.get("location") if isinstance(item.get("location"), dict) else {}
        place = Place(
            place_id=pid,
            name=str(name) if name is not None else "",
            lat=loc.get("lat", item.get("latitude")),
            lng=loc.get("lng", item.get("longitude")),
            rating=place_rating,
            review_count=_first(item, _REVIEW_COUNT_KEYS),
            address=_first(item, _ADDRESS_KEYS),
            maps_url=_first(item, _URL_KEYS),
        )
        place.reviews.append(
            Review(
                place_id=pid,
                text=str(_first(item, _TEXT_KEYS) or ""),
                rating=review_rating,
                published=_first(item, _PUBLISHED_KEYS),
                author=author or None,
                author_id=curl,
                owner_reply=None,
                source=CONTRIBUTOR_SOURCE,
            )
        )
        return place
