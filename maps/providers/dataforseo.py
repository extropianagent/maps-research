"""DataForSEO provider: Google Maps SERP (live) and Business Data Google Reviews (task-based).

Docs relied on: https://docs.dataforseo.com/v3/serp/google/maps/live/advanced/ and
https://docs.dataforseo.com/v3/business_data/google/reviews/task_post/ (docs hosts were NOT reachable
from the build machine; shapes are from memory of the docs, then mocks).

UNVERIFIED on a live run (check first):
- `location_coordinate` format "lat,lng,<zoom>z" (the "z" suffix) and the zoom-to-area mapping used here.
- `depth` semantics for maps search (results returned per task) and for reviews (multiples of 10).
- reviews: `task_post` body keys (`place_id`, `language_code`, `depth`, `sort_by`="newest"), task id at
  tasks[0].id, readiness signalled by tasks[0].status_code 20000 with a non-empty result, and review
  item keys `review_text`, `timestamp`, `profile_name`, `profile_url`, `reviews_count`, `owner_answer`,
  `original_language`, `rating.value`.
- maps item keys `place_id` (must be ChIJ form), `rating.votes_count`, `latitude`, `longitude`,
  `category`, `url`/`domain`, `phone`.
"""

from __future__ import annotations

import math
import os
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

import httpx

from .. import policy
from ..budget import Budget
from ..geography import BBox
from ..models import Hit, Place, Query, Review
from .base import PolicyStop, ProviderError, SearchPage
from .http import make_client, request_timeout

BASE_URL = "https://api.dataforseo.com/v3"
OK = 20000


def zoom_for_bbox(bbox: BBox) -> int:
    """Map zoom (3-21) whose ~1000 px viewport roughly covers the box's larger side."""
    extent = max(bbox.width_m, bbox.height_m, 1.0)
    lat = bbox.center[0]
    z = math.log2(156543.03 * max(math.cos(math.radians(lat)), 0.01) * 1000 / extent)
    return max(3, min(21, round(z)))


def _iso(ts: str | None) -> str | None:
    """'2022-03-03 08:56:11 +00:00' -> ISO 8601; None when not parseable."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.strip()).isoformat()
    except ValueError:
        return None


class DataForSEOProvider:
    """Retrieval through DataForSEO (paid per task; refused in free budget mode)."""

    name = "dataforseo"

    def __init__(
        self,
        budget: Budget,
        *,
        login: str | None = None,
        password: str | None = None,
        client: httpx.Client | None = None,
        timeout: float = 300.0,
        poll_timeout: float = 120.0,
        poll_interval: float = 5.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.budget = budget
        self._login = login if login is not None else os.environ.get("DATAFORSEO_LOGIN", "")
        self._password = password if password is not None else os.environ.get("DATAFORSEO_PASSWORD", "")
        self._timeout = timeout
        self._client = client or make_client(timeout=timeout)
        self.poll_timeout = poll_timeout
        self.poll_interval = poll_interval
        self._sleep = sleep
        self._clock = clock

    def available(self) -> bool:
        return bool(self._login and self._password)

    # -- HTTP ---------------------------------------------------------------------------------

    def _call(self, method: str, path: str, body: Any = None) -> dict[str, Any]:
        if not self.available():
            raise ProviderError("dataforseo: DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD not set")
        policy.check_outbound_payload(self.name, body)
        try:
            resp = self._client.request(
                method,
                f"{BASE_URL}{path}",
                json=body,
                auth=(self._login, self._password),
                timeout=request_timeout(self.budget, self._timeout),
            )
        except httpx.HTTPError as e:
            raise ProviderError(f"dataforseo: request failed ({type(e).__name__})") from None
        if resp.status_code != 200:
            reason = policy.detect_enforcement(resp.status_code, policy.enforcement_text(resp.text))
            if reason:
                raise PolicyStop(self.name, f"HTTP {resp.status_code}: {reason}")
            raise ProviderError(f"dataforseo: HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError:
            raise ProviderError("dataforseo: response is not JSON") from None
        if isinstance(data, dict):
            # Enforcement wording in any status message wins over "malformed" and "task failed".
            tasks = data.get("tasks") if isinstance(data.get("tasks"), list) else []
            for holder in (data, *(t for t in tasks if isinstance(t, dict))):
                msg = holder.get("status_message")
                reason = policy.detect_enforcement(
                    200, policy.enforcement_text(msg if isinstance(msg, str) else "")
                )
                if reason:
                    raise PolicyStop(self.name, f"status {holder.get('status_code')}: {str(msg)[:200]}")
        if not isinstance(data, dict) or not isinstance(data.get("tasks"), list) or not data["tasks"]:
            raise ProviderError("dataforseo: unexpected payload (no tasks)")
        return data

    @staticmethod
    def _task(data: dict[str, Any], *, allow_pending: bool = False) -> dict[str, Any]:
        task = data["tasks"][0]
        code = task.get("status_code")
        if code != OK and not (allow_pending and code in (20100, 40601, 40602)):
            reason = policy.detect_enforcement(200, policy.enforcement_text(str(task.get("status_message"))))
            if reason:
                raise PolicyStop(DataForSEOProvider.name, f"task error {code}: {reason}")
            raise ProviderError(f"dataforseo: task error {code}: {str(task.get('status_message'))[:80]}")
        return task

    # -- Provider protocol --------------------------------------------------------------------

    def text_search(
        self, query: Query, *, bbox: BBox | None, level: int = 0, max_results: int = 60, **_: Any
    ) -> SearchPage:
        task: dict[str, Any] = {"keyword": query.text, "depth": max_results}
        if query.language:
            task["language_code"] = query.language
        else:
            task["language_code"] = "en"
        if bbox is not None:
            lat, lng = bbox.center
            task["location_coordinate"] = f"{lat:.7f},{lng:.7f},{zoom_for_bbox(bbox)}z"
        self.budget.charge(
            "dataforseo.maps_search",
            fingerprint=Budget.fingerprint("POST", f"{BASE_URL}/serp/google/maps/live/advanced", [task]),
        )
        data = self._call("POST", "/serp/google/maps/live/advanced", [task])
        result = self._task(data).get("result") or []
        items = (result[0].get("items") if result and isinstance(result[0], dict) else None) or []
        items = [i for i in items if isinstance(i, dict) and i.get("type", "maps_search") == "maps_search"]
        places: list[Place] = []
        for item in items:
            place_id = item.get("place_id")
            if not place_id:
                continue
            lat, lng = item.get("latitude"), item.get("longitude")
            if bbox is not None and not bbox.contains(lat, lng):
                continue
            rating = item.get("rating") or {}
            cats = [c for c in (item.get("additional_categories") or []) if isinstance(c, str)]
            primary = item.get("category")
            place = Place(
                place_id=place_id,
                name=item.get("title") or "",
                lat=lat,
                lng=lng,
                rating=rating.get("value"),
                review_count=rating.get("votes_count"),
                primary_type=primary,
                types=([primary] if primary else []) + cats,
                address=item.get("address"),
                website=item.get("url") or item.get("domain"),
                phone=item.get("phone"),
                maps_url=item.get("check_url"),
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
        body: dict[str, Any] = {
            "place_id": place_id,
            "depth": max(10, math.ceil(limit / 10) * 10),
            "sort_by": "newest",
        }
        if language:
            body["language_code"] = language
        path = "/business_data/google/reviews/task_post"
        self.budget.charge(
            "dataforseo.reviews",
            units=math.ceil(limit / 10),
            fingerprint=Budget.fingerprint("POST", f"{BASE_URL}{path}", [body]),
        )
        post = self._task(self._call("POST", path, [body]), allow_pending=True)
        task_id = post.get("id")
        if not task_id:
            raise ProviderError("dataforseo: task_post returned no task id")
        # Bounded by wall-clock time: the smaller of this provider's timeout and what the budget has left.
        allowed_s = min(self.poll_timeout, self.budget.remaining_elapsed_s())
        start = self._clock()
        path = f"/business_data/google/reviews/task_get/{task_id}"
        attempt = 0
        while True:
            attempt += 1
            # every poll is an HTTP request: charged (zero price); identical by nature, so the attempt
            # number is part of its fingerprint
            self.budget.charge(
                "dataforseo.poll",
                fingerprint=Budget.fingerprint("GET", f"{BASE_URL}{path}", {"attempt": attempt}),
            )
            task = self._task(self._call("GET", path), allow_pending=True)
            if task.get("status_code") == OK and task.get("result"):
                break
            if self._clock() - start >= allowed_s:
                raise ProviderError(
                    f"dataforseo: reviews task {task_id} not ready before timeout "
                    "(already paid for; fetch it later with that task id)"
                )
            self._sleep(self.poll_interval)
            if self._clock() - start >= allowed_s:
                raise ProviderError(
                    f"dataforseo: reviews task {task_id} not ready before timeout "
                    "(already paid for; fetch it later with that task id)"
                )
        items = task["result"][0].get("items") or []
        out: list[Review] = []
        for r in items:
            if not isinstance(r, dict):
                continue
            out.append(
                Review(
                    place_id=place_id,
                    text=r.get("review_text") or "",
                    rating=(r.get("rating") or {}).get("value"),
                    language=r.get("original_language"),
                    published=_iso(r.get("timestamp")),
                    author=r.get("profile_name"),
                    author_id=r.get("profile_url"),
                    author_review_count=r.get("reviews_count"),
                    owner_reply=r.get("owner_answer"),
                    source=self.name,
                )
            )
        return out[:limit]
