"""Tests for DataForSEOProvider using httpx.MockTransport (no network)."""

from __future__ import annotations

import json

import httpx
import pytest

from maps.budget import Budget, BudgetExceeded, Ledger
from maps.geography import BBox
from maps.models import Query
from maps.providers.base import Provider, ProviderError
from maps.providers.dataforseo import DataForSEOProvider, zoom_for_bbox

BBOX = BBox(24.0, 46.0, 25.0, 47.0)


def mitem(pid: str | None, lat: float = 24.5, lng: float = 46.5) -> dict:
    d = {
        "type": "maps_search",
        "title": f"T-{pid}",
        "rating": {"value": 4.2, "votes_count": 33},
        "latitude": lat,
        "longitude": lng,
        "category": "Bakery",
        "address": "A",
        "url": "http://s",
        "phone": "p",
    }
    if pid:
        d["place_id"] = pid
    return d


def serp(items: list[dict]) -> dict:
    return {"status_code": 20000, "tasks": [{"status_code": 20000, "result": [{"items": items}]}]}


def make(handler, mode="quality", **kw):
    budget = Budget(Ledger(":memory:"), mode=mode, max_usd=5 if mode == "quality" else 0)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    p = DataForSEOProvider(
        budget, login="u", password="PASSWORD", client=client, sleep=kw.pop("sleep", lambda s: None), **kw
    )
    return p, budget


def test_available(monkeypatch):
    monkeypatch.delenv("DATAFORSEO_LOGIN", raising=False)
    monkeypatch.delenv("DATAFORSEO_PASSWORD", raising=False)
    b = Budget(Ledger(":memory:"))
    assert not DataForSEOProvider(b).available()
    assert not DataForSEOProvider(b, login="u").available()
    p = DataForSEOProvider(b, login="u", password="p")
    assert p.available() and isinstance(p, Provider)


def test_zoom_decreases_with_size():
    assert zoom_for_bbox(BBox.around(24.5, 46.5, 500)) > zoom_for_bbox(BBOX)
    assert 3 <= zoom_for_bbox(BBox(-80, -170, 80, 170)) <= 21


def test_search_mapping_filter_hits_request():
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        seen["path"] = req.url.path
        seen["auth"] = req.headers["authorization"]
        return httpx.Response(
            200, json=serp([mitem("ChIJa"), mitem(None), mitem("ChIJo", lat=10), mitem("ChIJb", lng=46.7)])
        )

    p, budget = make(handler)
    page = p.text_search(Query("bakery", "ar"), bbox=BBOX, level=1, max_results=20)
    assert [x.place_id for x in page.places] == ["ChIJa", "ChIJb"]
    a = page.places[0]
    assert (a.rating, a.review_count, a.lat) == (4.2, 33, 24.5)
    assert (a.primary_type, a.website) == ("Bakery", "http://s")
    h = page.places[1].hits[0]
    assert (h.rank, h.provider, h.language, h.cell, h.level) == (2, "dataforseo", "ar", BBOX.key(), 1)
    assert not page.saturated
    t = seen["body"][0]
    assert seen["path"] == "/v3/serp/google/maps/live/advanced"
    assert t["keyword"] == "bakery" and t["language_code"] == "ar" and t["depth"] == 20
    assert t["location_coordinate"].startswith("24.5000000,46.5000000,")
    assert "PASSWORD" not in json.dumps(seen["body"]) and seen["auth"].startswith("Basic ")
    assert budget.run_units["dataforseo.maps_search"] == 1


def test_saturated():
    p, _ = make(lambda r: httpx.Response(200, json=serp([mitem("ChIJ1"), mitem("ChIJ2")])))
    assert p.text_search(Query("x"), bbox=None, max_results=2).saturated


def test_http_and_task_errors():
    p, _ = make(lambda r: httpx.Response(401, text="no"))
    with pytest.raises(ProviderError) as e:
        p.text_search(Query("x"), bbox=BBOX)
    assert "PASSWORD" not in str(e.value)
    bad = {"tasks": [{"status_code": 40501, "status_message": "Invalid Field"}]}
    p, _ = make(lambda r: httpx.Response(200, json=bad))
    with pytest.raises(ProviderError):
        p.text_search(Query("x"), bbox=BBOX)
    p, _ = make(lambda r: httpx.Response(200, json={"oops": 1}))
    with pytest.raises(ProviderError):
        p.text_search(Query("x"), bbox=BBOX)


def test_free_mode_refused_before_http():
    calls = []
    p, _ = make(lambda r: calls.append(r) or httpx.Response(200, json=serp([])), mode="free")
    with pytest.raises(BudgetExceeded):
        p.text_search(Query("x"), bbox=BBOX)
    with pytest.raises(BudgetExceeded):
        p.reviews("ChIJa")
    assert calls == []


REVIEW = {
    "rating": {"value": 4},
    "review_text": "nice",
    "timestamp": "2022-03-03 08:56:11 +00:00",
    "profile_name": "Sam",
    "profile_url": "http://g/c/9",
    "reviews_count": 12,
    "owner_answer": "ty",
    "original_language": "en",
}


def test_reviews_post_poll_mapping():
    state = {"gets": 0}

    def handler(req):
        if req.method == "POST":
            assert req.url.path.endswith("/business_data/google/reviews/task_post")
            body = json.loads(req.content)[0]
            assert body["place_id"] == "ChIJa" and body["depth"] == 10
            return httpx.Response(200, json={"tasks": [{"status_code": 20100, "id": "T1"}]})
        assert req.url.path.endswith("/task_get/T1")
        state["gets"] += 1
        if state["gets"] < 3:
            return httpx.Response(200, json={"tasks": [{"status_code": 40602, "status_message": "queue"}]})
        return httpx.Response(200, json={"tasks": [{"status_code": 20000, "result": [{"items": [REVIEW]}]}]})

    sleeps = []
    p, budget = make(handler, sleep=sleeps.append)
    (r,) = p.reviews("ChIJa", limit=3)
    assert (r.text, r.rating, r.language, r.published, r.author, r.author_id) == (
        "nice",
        4,
        "en",
        "2022-03-03T08:56:11+00:00",
        "Sam",
        "http://g/c/9",
    )
    assert (r.author_review_count, r.owner_reply, r.source) == (12, "ty", "dataforseo")
    assert len(sleeps) == 2 and state["gets"] == 3
    assert budget.run_units["dataforseo.reviews"] == 1


def test_reviews_poll_timeout():
    def handler(req):
        if req.method == "POST":
            return httpx.Response(200, json={"tasks": [{"status_code": 20100, "id": "T1"}]})
        return httpx.Response(200, json={"tasks": [{"status_code": 40602}]})

    p, _ = make(handler, poll_timeout=10, poll_interval=5)
    with pytest.raises(ProviderError, match="timeout") as e:
        p.reviews("ChIJa")
    assert "T1" in str(e.value), "the task id lets the owner fetch the result later instead of paying again"


def test_text_search_ignores_extra_keyword_arguments():
    p, _ = make(lambda r: httpx.Response(200, json=serp([mitem("ChIJ1")])))
    page = p.text_search(Query("x"), bbox=None, min_rating=4.0, included_type="bakery", tier="pro")
    assert len(page.places) == 1


def test_http_error_does_not_leak_credentials():
    budget = Budget(Ledger(":memory:"), mode="quality", max_usd=5)
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(401, text="bad login LOGIN-ABC PASS-XYZ"))
    )
    p = DataForSEOProvider(budget, login="LOGIN-ABC", password="PASS-XYZ", client=client)
    with pytest.raises(ProviderError) as e:
        p.text_search(Query("x"), bbox=None)
    assert "401" in str(e.value) and "LOGIN-ABC" not in str(e.value) and "PASS-XYZ" not in str(e.value)


# -- account protection ---------------------------------------------------------------------


def test_suspension_is_a_policy_stop_but_a_bad_login_is_an_ordinary_error():
    from maps.providers.base import PolicyStop

    p, _ = make(lambda req: httpx.Response(403, text="account suspended"))
    with pytest.raises(PolicyStop) as e:
        p.text_search(Query("bakery"), bbox=BBOX)
    assert e.value.provider == "dataforseo"
    p, _ = make(lambda req: httpx.Response(401, text="Authorization failed"))
    with pytest.raises(ProviderError) as e2:
        p.text_search(Query("bakery"), bbox=BBOX)
    assert not isinstance(e2.value, PolicyStop)


def test_task_level_enforcement_message_is_a_policy_stop():
    from maps.providers.base import PolicyStop

    def handler(req):
        return httpx.Response(
            200,
            json={"tasks": [{"status_code": 40200, "status_message": "Account banned for terms violation"}]},
        )

    p, _ = make(handler)
    with pytest.raises(PolicyStop):
        p.text_search(Query("bakery"), bbox=BBOX)


def test_outbound_body_with_credentials_is_refused_before_any_request():
    from maps import policy

    seen = []
    p, _ = make(lambda req: seen.append(req) or httpx.Response(200, json=serp([])))
    with pytest.raises(policy.PolicyViolation):
        p._call("POST", "/x", [{"keyword": "x", "cookie": "SID=1"}])
    assert seen == []
