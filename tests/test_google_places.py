"""Tests for GooglePlaces (Places API New) using httpx.MockTransport (no network)."""

from __future__ import annotations

import json

import httpx
import pytest

from maps.budget import Budget, BudgetExceeded, Ledger
from maps.geography import BBox
from maps.models import Query
from maps.providers.base import Provider, ProviderError
from maps.providers.google_places import GooglePlaces

BBOX = BBox(24.0, 46.0, 25.0, 47.0)
KEY = "SECRET-KEY-123"


def gplace(pid: str, **kw) -> dict:
    d = {
        "id": pid,
        "displayName": {"text": f"N-{pid}", "languageCode": "en"},
        "formattedAddress": "1 Main St",
        "location": {"latitude": 24.5, "longitude": 46.5},
        "types": ["bakery", "food"],
        "primaryType": "bakery",
        "businessStatus": "OPERATIONAL",
        "googleMapsUri": f"https://maps.google.com/?cid={pid}",
        "rating": 4.4,
        "userRatingCount": 12,
        "priceLevel": "PRICE_LEVEL_MODERATE",
        "websiteUri": "https://w.example",
        "nationalPhoneNumber": "012 345",
    }
    d.update(kw)
    return d


def make(handler, *, budget=None, **kw):
    budget = budget or Budget(Ledger(":memory:"))
    client = httpx.Client(transport=httpx.MockTransport(handler))
    sleeps: list[float] = []
    kw.setdefault("api_key", KEY)
    p = GooglePlaces(budget, client=client, sleep=sleeps.append, **kw)
    p.sleeps = sleeps  # type: ignore[attr-defined]
    return p, budget


def paged(pages: list[dict], seen: list[httpx.Request] | None = None):
    """Handler serving `pages` in order; a page's 'token' key becomes nextPageToken."""
    it = iter(pages)

    def handler(req: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(req)
        page = next(it)
        body: dict = {"places": page["places"]}
        if page.get("token"):
            body["nextPageToken"] = page["token"]
        return httpx.Response(200, json=body)

    return handler


def ids(prefix: str, n: int) -> list[dict]:
    return [gplace(f"{prefix}{i}") for i in range(n)]


class FailingBudget(Budget):
    """Allows `allow` charges, then refuses."""

    def __init__(self, allow: int) -> None:
        super().__init__(Ledger(":memory:"))
        self.allow = allow
        self.charged: list[str] = []

    def charge(self, sku_name, units=1):
        if len(self.charged) >= self.allow:
            raise BudgetExceeded("nope")
        self.charged.append(sku_name)
        return super().charge(sku_name, units)


# -- basics ---------------------------------------------------------------------------------


def test_available(monkeypatch):
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    b = Budget(Ledger(":memory:"))
    assert not GooglePlaces(b).available()
    assert GooglePlaces(b, api_key="x").available()
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "y")
    p = GooglePlaces(b)
    assert p.available() and isinstance(p, Provider) and p.name == "google"


def test_unknown_tier():
    b = Budget(Ledger(":memory:"))
    with pytest.raises(ValueError):
        GooglePlaces(b, api_key="x", tier="platinum")
    p, _ = make(paged([{"places": []}]))
    with pytest.raises(ValueError):
        p.text_search(Query("x"), bbox=None, tier="platinum")


# -- request shape --------------------------------------------------------------------------


def test_request_shape():
    seen: list[httpx.Request] = []
    p, _ = make(paged([{"places": [gplace("a")]}], seen))
    p.text_search(Query("bakery", "ar"), bbox=BBOX, min_rating=4.2, included_type="bakery")
    (req,) = seen
    assert req.method == "POST"
    assert str(req.url) == "https://places.googleapis.com/v1/places:searchText"
    assert KEY not in str(req.url)
    assert req.headers["x-goog-api-key"] == KEY
    mask = req.headers["x-goog-fieldmask"]
    assert "nextPageToken" in mask and "places.rating" in mask and "places.nationalPhoneNumber" in mask
    body = json.loads(req.content)
    assert body == {
        "textQuery": "bakery",
        "pageSize": 20,
        "languageCode": "ar",
        "locationRestriction": {
            "rectangle": {
                "low": {"latitude": 24.0, "longitude": 46.0},
                "high": {"latitude": 25.0, "longitude": 47.0},
            }
        },
        "minRating": 4.2,
        "includedType": "bakery",
        "strictTypeFiltering": True,
    }


def test_minimal_body_omits_optionals():
    seen: list[httpx.Request] = []
    p, _ = make(paged([{"places": []}], seen))
    p.text_search(Query("bakery"), bbox=None)
    assert json.loads(seen[0].content) == {"textQuery": "bakery", "pageSize": 20}


@pytest.mark.parametrize(
    "tier,sku,has_pro,has_ent",
    [
        ("ids", "google.text_search.ids", False, False),
        ("pro", "google.text_search.pro", True, False),
        ("enterprise", "google.text_search.enterprise", True, True),
    ],
)
def test_tier_mask_and_sku(tier, sku, has_pro, has_ent):
    seen: list[httpx.Request] = []
    p, budget = make(paged([{"places": [gplace("a")]}], seen))
    p.text_search(Query("x"), bbox=None, tier=tier)
    mask = seen[0].headers["x-goog-fieldmask"].split(",")
    assert "places.id" in mask and "nextPageToken" in mask
    assert ("places.displayName" in mask) is has_pro
    assert ("places.rating" in mask) is has_ent
    assert budget.run_units == {sku: 1}


def test_constructor_tier_is_default():
    seen: list[httpx.Request] = []
    p, budget = make(paged([{"places": []}], seen), tier="ids")
    p.text_search(Query("x"), bbox=None)
    assert budget.run_units == {"google.text_search.ids": 1}


# -- pagination -----------------------------------------------------------------------------


def test_pagination_three_pages_same_body_and_charges():
    seen: list[httpx.Request] = []
    p, budget = make(
        paged(
            [
                {"places": ids("a", 20), "token": "T1"},
                {"places": ids("b", 20), "token": "T2"},
                {"places": ids("c", 20)},
            ],
            seen,
        )
    )
    page = p.text_search(Query("x", "en"), bbox=BBOX, min_rating=4.0)
    assert len(page.places) == 60 and len(seen) == 3
    bodies = [json.loads(r.content) for r in seen]
    assert "pageToken" not in bodies[0]
    assert [b.pop("pageToken", None) for b in bodies[1:]] == ["T1", "T2"]
    assert bodies[0] == bodies[1] == bodies[2]
    assert budget.run_units == {"google.text_search.enterprise": 3}


def test_saturated_at_cap_even_without_token():
    pages = [{"places": ids("a", 20), "token": "T1"}, {"places": ids("b", 20), "token": "T2"},
             {"places": ids("c", 20)}]
    p, _ = make(paged(pages))
    assert p.text_search(Query("x"), bbox=None).saturated


def test_not_saturated_when_last_page_short_and_no_token():
    p, _ = make(paged([{"places": ids("a", 20), "token": "T1"}, {"places": ids("b", 7)}]))
    page = p.text_search(Query("x"), bbox=None)
    assert len(page.places) == 27 and not page.saturated


def test_max_results_20_one_page_saturated_if_token():
    seen: list[httpx.Request] = []
    p, budget = make(paged([{"places": ids("a", 20), "token": "T1"}], seen))
    page = p.text_search(Query("x"), bbox=None, max_results=20)
    assert len(seen) == 1 and len(page.places) == 20 and page.saturated
    assert budget.run_units == {"google.text_search.enterprise": 1}


def test_max_results_20_not_saturated_without_token():
    p, _ = make(paged([{"places": ids("a", 20)}]))
    assert not p.text_search(Query("x"), bbox=None, max_results=20).saturated


def test_page_size_shrinks_to_remaining():
    seen: list[httpx.Request] = []
    p, _ = make(paged([{"places": ids("a", 20), "token": "T"}, {"places": ids("b", 5)}], seen))
    page = p.text_search(Query("x"), bbox=None, max_results=25)
    assert [json.loads(r.content)["pageSize"] for r in seen] == [20, 5]
    assert len(page.places) == 25


def test_max_results_capped_at_60():
    pages = [{"places": ids(c, 20), "token": "T"} for c in "abc"] + [{"places": ids("d", 20)}]
    seen: list[httpx.Request] = []
    p, _ = make(paged(pages, seen))
    page = p.text_search(Query("x"), bbox=None, max_results=500)
    assert len(page.places) == 60 and len(seen) == 3


def test_empty_result():
    p, _ = make(lambda r: httpx.Response(200, json={}))
    page = p.text_search(Query("x"), bbox=BBOX)
    assert page.places == [] and not page.saturated


# -- mapping --------------------------------------------------------------------------------


def test_place_mapping_and_hits_rank_across_pages():
    p, _ = make(paged([{"places": ids("a", 20), "token": "T"}, {"places": [gplace("z")]}]))
    page = p.text_search(Query("bakery", "en"), bbox=BBOX, level=3)
    a, z = page.places[0], page.places[-1]
    assert (a.place_id, a.name, a.lat, a.lng) == ("a0", "N-a0", 24.5, 46.5)
    assert (a.rating, a.review_count, a.primary_type, a.types) == (4.4, 12, "bakery", ["bakery", "food"])
    assert (a.address, a.price_level, a.business_status) == ("1 Main St", "PRICE_LEVEL_MODERATE", "OPERATIONAL")
    assert (a.website, a.phone, a.maps_url) == ("https://w.example", "012 345", "https://maps.google.com/?cid=a0")
    h = a.hits[0]
    assert (h.provider, h.query, h.language, h.cell, h.level, h.rank) == (
        "google", "bakery", "en", BBOX.key(), 3, 1)
    assert [pl.hits[0].rank for pl in page.places] == list(range(1, 22))
    assert z.place_id == "z" and z.hits[0].rank == 21


def test_sparse_place_and_no_bbox_cell():
    p, _ = make(paged([{"places": [{"id": "only"}]}]))
    (pl,) = p.text_search(Query("x"), bbox=None).places
    assert pl.place_id == "only" and pl.name == "" and pl.rating is None and pl.types == []
    assert pl.hits[0].cell is None and pl.hits[0].language is None


# -- budget ---------------------------------------------------------------------------------


def test_budget_refusal_on_page_2_returns_page_1_saturated():
    calls: list[httpx.Request] = []
    b = FailingBudget(allow=1)
    p, _ = make(paged([{"places": ids("a", 20), "token": "T"}, {"places": ids("b", 20)}], calls), budget=b)
    page = p.text_search(Query("x"), bbox=None)
    assert len(page.places) == 20 and page.saturated and len(calls) == 1
    assert page.budget_exhausted and page.stopped_early == "nope"


def test_http_error_on_page_2_returns_page_1_saturated_and_says_why():
    def handler(req):
        if b"pageToken" in req.content:
            return httpx.Response(500, json={"error": {"message": "backend down"}})
        return httpx.Response(200, json={"places": ids("a", 20), "nextPageToken": "T"})

    p, budget = make(handler)
    page = p.text_search(Query("x"), bbox=None)
    assert len(page.places) == 20 and page.saturated
    assert not page.budget_exhausted and "HTTP 500" in page.stopped_early


def test_http_error_on_page_1_still_raises():
    p, _ = make(lambda r: httpx.Response(500, text="x"))
    with pytest.raises(ProviderError):
        p.text_search(Query("x"), bbox=None)


def test_a_complete_search_reports_no_early_stop():
    p, _ = make(paged([{"places": ids("a", 3)}]))
    page = p.text_search(Query("x"), bbox=None)
    assert page.stopped_early is None and not page.budget_exhausted


def test_budget_refusal_on_page_1_raises():
    calls: list[httpx.Request] = []
    p, _ = make(paged([{"places": []}], calls), budget=FailingBudget(allow=0))
    with pytest.raises(BudgetExceeded):
        p.text_search(Query("x"), bbox=None)
    assert calls == []


# -- errors ---------------------------------------------------------------------------------


def test_http_error_carries_status_and_message_not_key():
    def handler(req):
        return httpx.Response(403, json={"error": {"code": 403, "message": f"API key {KEY} not valid"}})

    p, _ = make(handler)
    with pytest.raises(ProviderError) as e:
        p.text_search(Query("x"), bbox=None)
    msg = str(e.value)
    assert "403" in msg and "not valid" in msg and KEY not in msg


def test_http_error_without_json_body():
    p, _ = make(lambda r: httpx.Response(500, text="boom"))
    with pytest.raises(ProviderError, match="500"):
        p.text_search(Query("x"), bbox=None)


def test_network_error_wrapped_without_key():
    def handler(req):
        raise httpx.ConnectError(f"cannot reach {req.url} with {KEY}")

    p, _ = make(handler)
    with pytest.raises(ProviderError) as e:
        p.text_search(Query("x"), bbox=None)
    assert KEY not in str(e.value)


def test_missing_key_is_provider_error(monkeypatch):
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    calls: list[httpx.Request] = []
    p, budget = make(paged([{"places": []}], calls), api_key=None)
    with pytest.raises(ProviderError):
        p.text_search(Query("x"), bbox=None)
    assert calls == [] and budget.run_units == {}


@pytest.mark.parametrize("status", [429, 503])
def test_retry_once_single_charge(status):
    n = {"i": 0}

    def handler(req):
        n["i"] += 1
        if n["i"] == 1:
            return httpx.Response(status, json={"error": {"message": "slow down"}})
        return httpx.Response(200, json={"places": [gplace("a")]})

    p, budget = make(handler)
    page = p.text_search(Query("x"), bbox=None)
    assert len(page.places) == 1 and n["i"] == 2
    assert p.sleeps == [2]  # type: ignore[attr-defined]
    assert budget.run_units == {"google.text_search.enterprise": 1}


def test_retry_gives_up_after_one():
    n = {"i": 0}

    def handler(req):
        n["i"] += 1
        return httpx.Response(429, json={"error": {"message": "quota"}})

    p, _ = make(handler)
    with pytest.raises(ProviderError, match="429"):
        p.text_search(Query("x"), bbox=None)
    assert n["i"] == 2


# -- details / reviews ----------------------------------------------------------------------


def test_details_request_and_mapping():
    seen: list[httpx.Request] = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json=gplace("ChIJa"))

    p, budget = make(handler)
    pl = p.details("ChIJa", language="ar")
    (req,) = seen
    assert req.method == "GET" and req.url.path == "/v1/places/ChIJa"
    assert req.url.params["languageCode"] == "ar" and KEY not in str(req.url)
    assert req.headers["x-goog-api-key"] == KEY
    mask = req.headers["x-goog-fieldmask"].split(",")
    assert "id" in mask and "rating" in mask and "nextPageToken" not in mask
    assert not any(m.startswith("places.") for m in mask)
    assert (pl.place_id, pl.name, pl.rating, pl.phone) == ("ChIJa", "N-ChIJa", 4.4, "012 345")
    assert budget.run_units == {"google.details.enterprise": 1}


@pytest.mark.parametrize("tier", ["ids", "pro", "enterprise"])
def test_details_sku_per_tier(tier):
    p, budget = make(lambda r: httpx.Response(200, json={"id": "ChIJa"}))
    p.details("ChIJa", tier=tier)
    assert budget.run_units == {f"google.details.{tier}": 1}


def test_details_without_language_has_no_param():
    seen: list[httpx.Request] = []
    p, _ = make(lambda r: seen.append(r) or httpx.Response(200, json={"id": "ChIJa"}))
    p.details("ChIJa")
    assert "languageCode" not in seen[0].url.params


def test_reviews_mapping_prefers_original_text():
    seen: list[httpx.Request] = []
    review = {
        "rating": 5,
        "text": {"text": "great (translated)", "languageCode": "en"},
        "originalText": {"text": "ممتاز", "languageCode": "ar"},
        "publishTime": "2024-05-01T10:00:00Z",
        "authorAttribution": {"displayName": "Sam", "uri": "https://maps.google.com/contrib/1"},
        "googleMapsUri": "https://maps.google.com/r/1",
        "flagContentUri": "https://maps.google.com/flag/1",
    }
    second = {"rating": 2, "text": {"text": "meh", "languageCode": "en"}}

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"id": "ChIJa", "reviews": [review, second, second]})

    p, budget = make(handler)
    out = p.reviews("ChIJa", limit=2, language="en")
    (req,) = seen
    assert req.headers["x-goog-fieldmask"] == "id,reviews" and req.url.params["languageCode"] == "en"
    assert len(out) == 2
    r = out[0]
    assert (r.place_id, r.rating, r.text, r.language, r.published) == (
        "ChIJa", 5, "ممتاز", "ar", "2024-05-01T10:00:00Z")
    assert (r.author, r.author_id, r.url, r.flag_url, r.source) == (
        "Sam", "https://maps.google.com/contrib/1", "https://maps.google.com/r/1",
        "https://maps.google.com/flag/1", "google")
    assert (out[1].text, out[1].language) == ("meh", "en")
    assert budget.run_units == {"google.details.atmosphere": 1}


def test_reviews_none_returned():
    p, _ = make(lambda r: httpx.Response(200, json={"id": "ChIJa"}))
    assert p.reviews("ChIJa") == []


# -- resolve_area ---------------------------------------------------------------------------


def test_resolve_area_success():
    seen: list[httpx.Request] = []
    body = {"places": [{
        "id": "ChIJr", "displayName": {"text": "Riyadh"}, "formattedAddress": "Riyadh Saudi Arabia",
        "location": {"latitude": 24.7, "longitude": 46.7},
        "viewport": {"low": {"latitude": 24.0, "longitude": 46.0}, "high": {"latitude": 25.0, "longitude": 47.0}},
    }]}
    p, budget = make(lambda r: seen.append(r) or httpx.Response(200, json=body))
    bbox, label = p.resolve_area("Riyadh")
    assert bbox == BBOX and label == "Riyadh, Riyadh Saudi Arabia"
    assert json.loads(seen[0].content) == {"textQuery": "Riyadh", "pageSize": 1}
    assert seen[0].headers["x-goog-fieldmask"] == (
        "places.id,places.displayName,places.formattedAddress,places.location,places.viewport")
    assert budget.run_units == {"google.text_search.pro": 1}


def test_resolve_area_no_result():
    p, _ = make(lambda r: httpx.Response(200, json={}))
    with pytest.raises(ProviderError, match="Nowhere"):
        p.resolve_area("Nowhere")


def test_resolve_area_no_viewport():
    p, _ = make(lambda r: httpx.Response(200, json={"places": [{"id": "x", "displayName": {"text": "X"}}]}))
    with pytest.raises(ProviderError, match="viewport"):
        p.resolve_area("X")
