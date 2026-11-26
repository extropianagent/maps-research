"""Tests for PlacesAggregate using httpx.MockTransport (no network)."""

from __future__ import annotations

import json

import httpx
import pytest

from maps.budget import Budget, BudgetExceeded, Ledger
from maps.geography import BBox
from maps.providers.base import ProviderError
from maps.providers.google_aggregate import EnumerationResult, PlacesAggregate

BBOX = BBox(24.0, 46.0, 25.0, 47.0)
KEY = "SECRET-KEY-123"
URL = "https://areainsights.googleapis.com/v1:computeInsights"


def box_of(body: dict) -> BBox:
    ring = body["filter"]["locationFilter"]["customArea"]["polygon"]["coordinates"]
    return BBox(ring[0]["latitude"], ring[0]["longitude"], ring[2]["latitude"], ring[2]["longitude"])


def make(handler, budget=None):
    budget = budget or Budget(Ledger(":memory:"))
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return PlacesAggregate(budget, api_key=KEY, client=client), budget


def grid(counts: dict[str, int], places: dict[str, list[str]] | None = None, seen: list | None = None):
    """Handler answering counts/place lists per cell key; unknown cells count 0."""
    places = places or {}

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        key = box_of(body).key()
        if seen is not None:
            seen.append((body["insights"][0], key))
        if body["insights"] == ["INSIGHT_COUNT"]:
            return httpx.Response(200, json={"count": str(counts.get(key, 0))})
        return httpx.Response(
            200, json={"placeInsights": [{"place": f"places/{i}"} for i in places.get(key, [])]}
        )

    return handler


def test_available(monkeypatch):
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    b = Budget(Ledger(":memory:"))
    assert not PlacesAggregate(b).available()
    assert PlacesAggregate(b, api_key="x").available()
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "y")
    assert PlacesAggregate(b).available()


def test_count_request_shape_and_string_count():
    seen: list[httpx.Request] = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"count": "123"})

    agg, budget = make(handler)
    assert agg.count(BBOX, types=["bakery"], min_rating=1.0, max_rating=3.5) == 123
    (req,) = seen
    assert req.method == "POST" and str(req.url) == URL and KEY not in str(req.url)
    assert req.headers["x-goog-api-key"] == KEY
    body = json.loads(req.content)
    assert body["insights"] == ["INSIGHT_COUNT"]
    f = body["filter"]
    ring = f["locationFilter"]["customArea"]["polygon"]["coordinates"]
    assert ring[0] == ring[-1] == {"latitude": 24.0, "longitude": 46.0}
    assert ring[2] == {"latitude": 25.0, "longitude": 47.0} and len(ring) == 5
    assert f["typeFilter"] == {"includedTypes": ["bakery"]}
    assert f["operatingStatus"] == ["OPERATING_STATUS_OPERATIONAL"]
    assert f["ratingFilter"] == {"minRating": 1.0, "maxRating": 3.5}
    assert budget.run_units == {"google.aggregate": 1}


def test_filters_omitted_when_not_given():
    seen: list[httpx.Request] = []
    agg, _ = make(lambda r: seen.append(r) or httpx.Response(200, json={"count": 7}))
    assert agg.count(BBOX, types=["cafe"], operational_only=False) == 7
    f = json.loads(seen[0].content)["filter"]
    assert "operatingStatus" not in f and "ratingFilter" not in f


def test_single_rating_bound_only():
    seen: list[httpx.Request] = []
    agg, _ = make(lambda r: seen.append(r) or httpx.Response(200, json={}))
    agg.count(BBOX, types=["cafe"], max_rating=2.0)
    assert json.loads(seen[0].content)["filter"]["ratingFilter"] == {"maxRating": 2.0}


def test_missing_count_is_zero():
    agg, _ = make(lambda r: httpx.Response(200, json={}))
    assert agg.count(BBOX, types=["cafe"]) == 0


def test_place_ids_request_and_parsing():
    seen: list[httpx.Request] = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"placeInsights": [{"place": "places/ChIJa"}, {"place": "places/ChIJb"}]})

    agg, budget = make(handler)
    assert agg.place_ids(BBOX, types=["cafe"], min_rating=4.0) == ["ChIJa", "ChIJb"]
    assert json.loads(seen[0].content)["insights"] == ["INSIGHT_PLACES"]
    assert budget.run_units == {"google.aggregate": 1}


def test_place_ids_empty():
    agg, _ = make(lambda r: httpx.Response(200, json={}))
    assert agg.place_ids(BBOX, types=["cafe"]) == []


def test_http_error_message_without_key():
    def handler(req):
        return httpx.Response(403, json={"error": {"message": f"key {KEY} blocked"}})

    agg, _ = make(handler)
    with pytest.raises(ProviderError) as e:
        agg.count(BBOX, types=["cafe"])
    assert "403" in str(e.value) and "blocked" in str(e.value) and KEY not in str(e.value)


def test_network_error_wrapped():
    def handler(req):
        raise httpx.ConnectError(KEY)

    agg, _ = make(handler)
    with pytest.raises(ProviderError) as e:
        agg.count(BBOX, types=["cafe"])
    assert KEY not in str(e.value)


# -- enumerate ------------------------------------------------------------------------------


def test_small_root_fetches_ids_directly():
    seen: list = []
    agg, _ = make(grid({BBOX.key(): 3}, {BBOX.key(): ["a", "b", "c"]}, seen))
    res = agg.enumerate(BBOX, types=["cafe"])
    assert isinstance(res, EnumerationResult)
    assert (res.place_ids, res.total_count, res.requests, res.unresolved, res.stopped) == (
        ["a", "b", "c"], 3, 2, [], None)
    assert seen == [("INSIGHT_COUNT", BBOX.key()), ("INSIGHT_PLACES", BBOX.key())]


def test_zero_root_stops():
    agg, _ = make(grid({}))
    res = agg.enumerate(BBOX, types=["cafe"])
    assert res.place_ids == [] and res.total_count == 0 and res.requests == 1


def test_recursion_splits_dedups_and_skips_zero_cells():
    q = BBOX.split()
    keys = [c.key() for c in q]
    counts = {BBOX.key(): 150, keys[0]: 2, keys[1]: 3, keys[2]: 0, keys[3]: 1}
    places = {keys[0]: ["a", "b"], keys[1]: ["b", "c", "d"], keys[3]: ["e"]}
    seen: list = []
    agg, budget = make(grid(counts, places, seen))
    res = agg.enumerate(BBOX, types=["cafe"], min_rating=1.0, max_rating=3.0)
    assert res.place_ids == ["a", "b", "c", "d", "e"]  # "b" appears in two cells, kept once, first-seen order
    assert res.total_count == 150 and res.unresolved == [] and res.stopped is None
    assert ("INSIGHT_PLACES", BBOX.key()) not in seen
    assert ("INSIGHT_PLACES", keys[2]) not in seen  # zero-count cell never fetched
    assert [k for t, k in seen if t == "INSIGHT_COUNT"] == [BBOX.key(), *keys]
    assert res.requests == len(seen) == 1 + 4 + 3
    assert budget.run_units == {"google.aggregate": 8}


def test_exactly_100_is_fetched_not_split():
    seen: list = []
    agg, _ = make(grid({BBOX.key(): 100}, {BBOX.key(): ["a"]}, seen))
    res = agg.enumerate(BBOX, types=["cafe"])
    assert ("INSIGHT_PLACES", BBOX.key()) in seen and res.place_ids == ["a"]


def test_deeper_recursion():
    q = BBOX.split()
    sub = q[0].split()
    counts = {BBOX.key(): 400, q[0].key(): 101, sub[3].key(): 5}
    agg, _ = make(grid(counts, {sub[3].key(): ["x"]}))
    res = agg.enumerate(BBOX, types=["cafe"])
    assert res.place_ids == ["x"] and res.unresolved == []


def test_unresolved_at_max_level():
    q = BBOX.split()
    counts = {BBOX.key(): 500, q[0].key(): 150, q[1].key(): 4}
    agg, _ = make(grid(counts, {q[1].key(): ["a"]}))
    res = agg.enumerate(BBOX, types=["cafe"], max_level=1)
    assert res.place_ids == ["a"] and res.unresolved == [q[0].key()] and res.total_count == 500


def test_max_level_zero_root_unresolved():
    agg, _ = make(grid({BBOX.key(): 500}))
    res = agg.enumerate(BBOX, types=["cafe"], max_level=0)
    assert res.unresolved == [BBOX.key()] and res.place_ids == []


def test_budget_stop_returns_partial_with_unresolved():
    q = BBOX.split()
    keys = [c.key() for c in q]
    counts = {BBOX.key(): 150, keys[0]: 2, keys[1]: 3, keys[2]: 1, keys[3]: 1}
    places = {keys[0]: ["a", "b"], keys[1]: ["c"]}

    class Limited(Budget):
        def __init__(self, allow: int) -> None:
            super().__init__(Ledger(":memory:"))
            self.allow, self.n = allow, 0

        def charge(self, sku_name, units=1):
            if self.n >= self.allow:
                raise BudgetExceeded("free allowance gone")
            self.n += 1
            return super().charge(sku_name, units)

    # root count, q0 count, q0 ids, then refused at q1's count
    agg, _ = make(grid(counts, places), budget=Limited(3))
    res = agg.enumerate(BBOX, types=["cafe"])
    assert res.place_ids == ["a", "b"] and res.total_count == 150
    assert res.stopped == "free allowance gone"
    assert res.unresolved == keys[1:] and res.requests == 3


def test_budget_stop_on_root():
    class Never(Budget):
        def charge(self, sku_name, units=1):
            raise BudgetExceeded("no money")

    agg, _ = make(grid({}), budget=Never(Ledger(":memory:")))
    res = agg.enumerate(BBOX, types=["cafe"])
    assert res.stopped == "no money" and res.unresolved == [BBOX.key()] and res.requests == 0
