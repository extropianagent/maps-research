"""Tests for ApifyProvider using httpx.MockTransport (no network)."""

from __future__ import annotations

import json

import httpx
import pytest

from maps.budget import Budget, BudgetExceeded, Ledger
from maps.geography import BBox
from maps.models import Query
from maps.providers.apify import ApifyProvider
from maps.providers.base import Provider, ProviderError

BBOX = BBox(24.0, 46.0, 25.0, 47.0)


def item(pid: str | None, lat: float = 24.5, lng: float = 46.5, **kw) -> dict:
    d = {
        "title": f"T-{pid}",
        "location": {"lat": lat, "lng": lng},
        "totalScore": 4.5,
        "reviewsCount": 10,
        "categoryName": "Bakery",
        "categories": ["Bakery", "Cafe"],
        "address": "A",
        "website": "w",
        "phone": "p",
        "url": "u",
    }
    if pid:
        d["placeId"] = pid
    d.update(kw)
    return d


def make(handler, mode="quality", **kw):
    budget = Budget(Ledger(":memory:"), mode=mode, max_usd=5 if mode == "quality" else 0)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return ApifyProvider(budget, token=kw.pop("token", "tok"), client=client), budget


def test_available(monkeypatch):
    monkeypatch.delenv("APIFY_TOKEN", raising=False)
    b = Budget(Ledger(":memory:"))
    assert not ApifyProvider(b).available()
    assert ApifyProvider(b, token="x").available()
    monkeypatch.setenv("APIFY_TOKEN", "y")
    p = ApifyProvider(b)
    assert p.available() and isinstance(p, Provider)


def test_search_mapping_filter_hits_and_request():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["auth"] = req.headers["authorization"]
        seen["url"] = str(req.url)
        seen["body"] = json.loads(req.content)
        return httpx.Response(
            201, json=[item("ChIJa"), item(None), item("ChIJout", lat=50.0), item("ChIJb", lat=24.6)]
        )

    p, budget = make(handler)
    page = p.text_search(Query("bakery", "en"), bbox=BBOX, level=2, max_results=10)
    assert [x.place_id for x in page.places] == ["ChIJa", "ChIJb"]
    a = page.places[0]
    assert (a.name, a.rating, a.review_count, a.primary_type, a.types) == (
        "T-ChIJa",
        4.5,
        10,
        "Bakery",
        ["Bakery", "Cafe"],
    )
    assert a.hits[0].rank == 1 and page.places[1].hits[0].rank == 2
    h = a.hits[0]
    assert (h.provider, h.query, h.language, h.cell, h.level) == ("apify", "bakery", "en", BBOX.key(), 2)
    assert not page.saturated
    assert seen["auth"] == "Bearer tok" and "tok" not in seen["url"]
    assert seen["body"]["searchStringsArray"] == ["bakery"]
    assert seen["body"]["maxCrawledPlacesPerSearch"] == 10
    assert seen["body"]["customGeolocation"]["type"] == "Polygon"
    assert budget.run_units["apify.place"] == 10


def test_saturated():
    p, _ = make(lambda r: httpx.Response(201, json=[item("ChIJ1"), item("ChIJ2")]))
    assert p.text_search(Query("x"), bbox=None, max_results=2).saturated


def test_http_error_no_credentials_leak():
    p, _ = make(lambda r: httpx.Response(500, text="boom SECRET echoed"), token="SECRET")
    with pytest.raises(ProviderError) as e:
        p.text_search(Query("x"), bbox=BBOX)
    assert "HTTP 500" in str(e.value) and "SECRET" not in str(e.value)


def test_text_search_ignores_extra_keyword_arguments():
    p, _ = make(lambda r: httpx.Response(201, json=[item("ChIJ1")]))
    page = p.text_search(Query("x"), bbox=None, min_rating=4.0, included_type="bakery", tier="pro")
    assert len(page.places) == 1


def test_malformed_payload():
    p, _ = make(lambda r: httpx.Response(200, json={"not": "a list"}))
    with pytest.raises(ProviderError):
        p.text_search(Query("x"), bbox=BBOX)


def test_reviews_mapping():
    def handler(req):
        body = json.loads(req.content)
        assert body["placeIds"] == ["ChIJa"] and body["maxReviews"] == 3 and body["language"] == "ar"
        return httpx.Response(
            201,
            json=[
                {
                    "placeId": "ChIJa",
                    "reviews": [
                        {
                            "text": "great",
                            "stars": 5,
                            "publishedAtDate": "2024-05-01T10:00:00.000Z",
                            "name": "Sam",
                            "reviewerId": "1",
                            "reviewerUrl": "http://g/c/1",
                            "reviewerNumberOfReviews": 7,
                            "responseFromOwnerText": "thanks",
                            "originalLanguage": "ar",
                        }
                    ],
                }
            ],
        )

    p, budget = make(handler)
    (r,) = p.reviews("ChIJa", limit=3, language="ar")
    assert (r.text, r.rating, r.language, r.published, r.author, r.author_id) == (
        "great",
        5,
        "ar",
        "2024-05-01T10:00:00.000Z",
        "Sam",
        "http://g/c/1",
    )
    assert (r.author_review_count, r.owner_reply, r.source, r.place_id) == (7, "thanks", "apify", "ChIJa")
    assert budget.run_units["apify.review"] == 3


def test_free_mode_within_credit_works():
    p, budget = make(lambda r: httpx.Response(201, json=[item("ChIJa")]), mode="free")
    assert len(p.text_search(Query("x"), bbox=BBOX, max_results=20).places) == 1
    assert budget.run_usd == 0


def test_free_mode_refuses_beyond_credit_before_http():
    calls = []
    p, _ = make(lambda r: calls.append(r) or httpx.Response(201, json=[]), mode="free")
    with pytest.raises(BudgetExceeded):
        p.text_search(Query("x"), bbox=BBOX, max_results=5000)  # $20 > $5 credit
    assert calls == []
