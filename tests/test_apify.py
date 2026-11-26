"""Tests for ApifyProvider using httpx.MockTransport (no network)."""

from __future__ import annotations

import json

import httpx
import pytest

from maps.budget import Budget, BudgetExceeded, Ledger
from maps.geography import BBox
from maps.models import Query
from maps.providers.apify import ApifyProvider, parse_contributor
from maps.providers.base import Provider, ProviderError, QuotaExceeded

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


# -- contributor-first discovery ------------------------------------------------------------

CID = "112233445566778899001"
CURL = f"https://www.google.com/maps/contrib/{CID}/reviews"
PID_A = "ChIJaaaaaaaaaa"
PID_B = "ChIJbbbbbbbbbb"


def citem(pid: str | None, **kw) -> dict:
    d: dict = {"title": f"P-{pid}", "stars": 4, "totalScore": 4.6, "text": "nice"}
    if pid:
        d["placeId"] = pid
    d.update(kw)
    return d


def capture(items, seen: dict | None = None):
    seen = seen if seen is not None else {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["auth"] = req.headers["authorization"]
        seen["body"] = json.loads(req.content)
        return httpx.Response(201, json=items)

    return handler


@pytest.mark.parametrize(
    "value",
    [
        f"https://www.google.com/maps/contrib/{CID}",
        f"https://www.google.com/maps/contrib/{CID}/reviews",
        f"https://www.google.com/maps/contrib/{CID}/reviews?hl=en",
        f"https://www.google.com/maps/contrib/{CID}?hl=en",
        f"  {CID}  ",
    ],
)
def test_parse_contributor_accepts(value):
    assert parse_contributor(value) == (CID, CURL)


@pytest.mark.parametrize(
    "value", ["", "abc", "12345", "123456789", "https://www.google.com/maps/place/x", "x" + CID + "y"]
)
def test_parse_contributor_rejects(value):
    with pytest.raises(ValueError):
        parse_contributor(value)


def test_contributor_default_actor_and_input():
    seen: dict = {}
    p, _ = make(capture([citem(PID_A)], seen))
    p.contributor_reviews(CID, limit=7)
    assert (
        "/acts/lexis-solutions~google-maps-contributor-reviews-scraper/run-sync-get-dataset-items"
        in seen["url"]
    )
    assert seen["auth"] == "Bearer tok" and "tok" not in seen["url"]
    assert seen["body"] == {"startUrls": [{"url": CURL}], "maxItems": 7}


OLD_ENV_TEMPLATE = {
    "searchStringsArray": ["bakery Riyadh"],
    "maxCrawledPlacesPerSearch": 5000,
    "maxItems": 5000,
    "x": "{contributor_url}",
}


def test_the_old_contributor_env_vars_change_neither_url_nor_body(monkeypatch):
    monkeypatch.setenv("MAPS_APIFY_CONTRIBUTOR_ACTOR", "compass~crawler-google-places")
    monkeypatch.setenv("MAPS_APIFY_CONTRIBUTOR_INPUT", json.dumps(OLD_ENV_TEMPLATE))
    seen: dict = {}
    p, _ = make(capture([], seen))
    p.contributor_reviews(CID, limit=7)
    assert "/acts/lexis-solutions~google-maps-contributor-reviews-scraper/run-sync" in seen["url"]
    assert seen["body"] == {"startUrls": [{"url": CURL}], "maxItems": 7}


def test_contributor_actor_is_a_one_entry_allow_list_in_code():
    from maps.providers import apify

    assert apify.CONTRIBUTOR_ACTORS == ("lexis-solutions~google-maps-contributor-reviews-scraper",)
    assert apify.CONTRIBUTOR_ACTOR in apify.CONTRIBUTOR_ACTORS
    p, _ = make(capture([]))
    with pytest.raises(ProviderError, match="allow-list"):
        p._run({"startUrls": []}, "someone~else")


def test_no_apify_setting_comes_from_the_environment_except_the_token():
    import inspect
    import re

    from maps.providers import apify

    assert re.findall(r"environ[^\n]*", inspect.getsource(apify).split('"""', 2)[2]) == [
        'environ.get("APIFY_TOKEN", "")'
    ]


def test_contributor_invalid_id_raises_before_charge():
    p, budget = make(lambda r: httpx.Response(201, json=[]))
    with pytest.raises(ValueError):
        p.contributor_reviews("nope")
    assert budget.run_units == {}


def test_contributor_budget_charged_before_request():
    units_at_request = {}

    def handler(req):
        units_at_request.update(budget.run_units)
        return httpx.Response(201, json=[])

    p, budget = make(handler)
    p.contributor_reviews(CID, limit=12)
    assert units_at_request == {"apify.contributor": 12}


def test_contributor_free_mode_refuses_when_credit_used_up():
    calls: list = []
    p, budget = make(lambda r: calls.append(r) or httpx.Response(201, json=[]), mode="free")
    budget.ledger.add("apify.contributor", 4500, 4.5)  # 90% of the $5 monthly credit
    with pytest.raises(BudgetExceeded):
        p.contributor_reviews(CID, limit=10)
    assert calls == []


def test_contributor_mapping_stars_and_total_score():
    it = citem(
        PID_A,
        address="1 Main",
        reviewsCount=88,
        url="https://maps/x",
        location={"lat": 1.5, "lng": 2.5},
        updatedAt="2025-01-02",
        reviewerName="Sam",
        text="lovely",
        rating=1,  # ambiguous, must not win over totalScore
    )
    p, _ = make(capture([it]))
    (pl,) = p.contributor_reviews(CID)
    assert (pl.place_id, pl.name, pl.address, pl.rating, pl.review_count) == (
        PID_A,
        f"P-{PID_A}",
        "1 Main",
        4.6,
        88,
    )
    assert (pl.maps_url, pl.lat, pl.lng) == ("https://maps/x", 1.5, 2.5)
    (r,) = pl.reviews
    assert (r.rating, r.text, r.published, r.author, r.author_id, r.source, r.place_id) == (
        4,
        "lovely",
        "2025-01-02",
        "Sam",
        CURL,
        "apify_contributor",
        PID_A,
    )


def test_contributor_lat_lng_fallback_fields():
    p, _ = make(capture([citem(PID_A, latitude=3.0, longitude=4.0)]))
    (pl,) = p.contributor_reviews(CID)
    assert (pl.lat, pl.lng) == (3.0, 4.0)


def test_contributor_rating_is_review_when_no_place_rating_field():
    it = {"placeId": PID_A, "title": "T", "rating": 5}
    p, _ = make(capture([it]))
    (pl,) = p.contributor_reviews(CID)
    assert pl.reviews[0].rating == 5 and pl.rating is None


def test_contributor_rating_is_place_when_review_rating_field_present():
    it = {"placeId": PID_A, "title": "T", "reviewRating": 2, "rating": 4.3}
    p, _ = make(capture([it]))
    (pl,) = p.contributor_reviews(CID)
    assert pl.reviews[0].rating == 2 and pl.rating == 4.3


def test_contributor_rating_not_review_when_total_score_present():
    it = {"placeId": PID_A, "title": "T", "totalScore": 4.1, "rating": 4.1}
    p, _ = make(capture([it]))
    (pl,) = p.contributor_reviews(CID)
    assert pl.reviews[0].rating is None and pl.rating == 4.1


def test_contributor_name_is_author_only_when_clearly_reviewer():
    same = {"placeId": PID_A, "name": "Cafe", "stars": 3}
    other = {"placeId": PID_B, "title": "Cafe", "name": "Sam", "stars": 3}
    p, _ = make(capture([same, other]))
    a, b = p.contributor_reviews(CID)
    assert a.name == "Cafe" and a.reviews[0].author is None
    assert b.name == "Cafe" and b.reviews[0].author == "Sam"


def test_contributor_skips_items_without_valid_place_id():
    items = [citem(None), citem("short"), citem(PID_A), {"place_id": PID_B, "stars": 5}]
    p, _ = make(capture(items))
    assert [x.place_id for x in p.contributor_reviews(CID)] == [PID_A, PID_B]


def test_contributor_hits_and_limit():
    items = [citem(f"ChIJ{n:010d}") for n in range(5)]
    p, _ = make(capture(items))
    places = p.contributor_reviews(CID, limit=3)
    assert len(places) == 3
    for i, pl in enumerate(places, 1):
        (h,) = pl.hits
        assert (h.provider, h.query, h.rank) == ("apify_contributor", f"contributor:{CID}", i)
        assert pl.reviews[0].author_id == CURL


def test_contributor_http_error_no_token_leak():
    p, _ = make(lambda r: httpx.Response(500, text="boom SECRET"), token="SECRET")
    with pytest.raises(ProviderError) as e:
        p.contributor_reviews(CID)
    assert "HTTP 500" in str(e.value) and "SECRET" not in str(e.value)


def test_contributor_429_is_quota_exceeded():
    p, _ = make(lambda r: httpx.Response(429, text="slow down"))
    with pytest.raises(QuotaExceeded):
        p.contributor_reviews(CID)


# -- account protection ---------------------------------------------------------------------


def test_suspension_is_a_policy_stop_but_an_invalid_token_is_an_ordinary_error():
    from maps.providers.base import PolicyStop

    p, _ = make(lambda req: httpx.Response(403, text='{"error":"account has been suspended"}'))
    with pytest.raises(PolicyStop) as e:
        p.text_search(Query("bakery"), bbox=BBOX)
    assert e.value.provider == "apify"
    p, _ = make(lambda req: httpx.Response(403, text='{"error":"API key not valid"}'))
    with pytest.raises(ProviderError) as e2:
        p.text_search(Query("bakery"), bbox=BBOX)
    assert not isinstance(e2.value, PolicyStop)


def test_a_cookie_bearing_env_template_is_ignored_not_sent(monkeypatch):
    seen: dict = {}
    p, _ = make(capture([], seen))
    monkeypatch.setenv(
        "MAPS_APIFY_CONTRIBUTOR_INPUT",
        json.dumps({"startUrls": [{"url": "{contributor_url}"}], "cookies": [{"name": "SID"}]}),
    )
    p.contributor_reviews("1234567890123456789")
    assert "cookies" not in seen["body"] and "SID" not in json.dumps(seen["body"])


def test_search_payload_with_a_proxy_option_is_refused():
    from maps import policy

    p, _ = make(lambda req: httpx.Response(200, json=[]))
    with pytest.raises(policy.PolicyViolation, match="circumvention"):
        p._run({"searchStringsArray": ["x"], "proxyConfiguration": {"useApifyProxy": True}})
