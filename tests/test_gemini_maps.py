"""Tests for GeminiMaps (Grounding with Google Maps) using httpx.MockTransport (no network)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from maps.budget import Budget, BudgetExceeded, Ledger
from maps.geography import BBox
from maps.models import Query
from maps.providers.base import Provider, ProviderError, QuotaExceeded, SearchPage
from maps.providers.gemini_maps import DEFAULT_MODEL, PROMPT_TEMPLATE, GeminiMaps

pytestmark = pytest.mark.usefixtures("spending_none")


FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "gemini_generate_content_v1beta.json").read_text())
SCHEMAS: dict[str, Any] = FIXTURE["schemas"]
KEY = "SECRET-KEY-123"
BBOX = BBox(24.0, 46.0, 25.0, 47.0)


def chunk(pid: str | None, title: str = "T", uri: str = "https://maps.google.com/?cid=1", text=None) -> dict:
    maps: dict[str, Any] = {"title": title, "uri": uri}
    if pid is not None:
        maps["placeId"] = f"places/{pid}"
    if text is not None:
        maps["text"] = text
    return {"maps": maps}


def response(chunks: list[dict], text: str = "Answer.") -> dict:
    return {
        "candidates": [
            {
                "content": {"role": "model", "parts": [{"text": text}]},
                "groundingMetadata": {"groundingChunks": chunks},
            }
        ]
    }


class Server:
    def __init__(self, payload: dict | None = None, status: int = 200) -> None:
        self.payload = payload if payload is not None else response([chunk("P1")])
        self.status = status
        self.requests: list[httpx.Request] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        return httpx.Response(self.status, json=self.payload)

    def body(self) -> dict:
        return json.loads(self.requests[0].content)


def make(server: Any, *, budget: Budget | None = None, **kw: Any):
    budget = budget or Budget(Ledger(":memory:"))  # gemini is recorded as billing none: free-mode rules
    client = httpx.Client(transport=httpx.MockTransport(server))
    kw.setdefault("api_key", KEY)
    return GeminiMaps(budget, client=client, **kw), budget


# -- basics -----------------------------------------------------------------------------------


def test_available_and_protocol(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    b = Budget(Ledger(":memory:"))
    assert not GeminiMaps(b).available()
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    p = GeminiMaps(b)
    assert p.available() and isinstance(p, Provider) and p.name == "gemini_maps"


def test_missing_key_errors_before_charge_or_request(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    s = Server()
    p, budget = make(s, api_key=None)
    with pytest.raises(ProviderError, match="GEMINI_API_KEY"):
        p.search_places("x")
    assert s.requests == [] and budget.run_units == {}


def test_model_default_env_and_argument(monkeypatch):
    monkeypatch.delenv("MAPS_GEMINI_MODEL", raising=False)
    assert make(Server())[0].model == DEFAULT_MODEL == "gemini-flash-latest"
    monkeypatch.setenv("MAPS_GEMINI_MODEL", "gemini-x")
    assert make(Server())[0].model == "gemini-x"
    assert make(Server(), model="gemini-y")[0].model == "gemini-y"


# -- request shape, checked against the fixture ------------------------------------------------


def test_request_shape_and_fixture_agreement(monkeypatch):
    monkeypatch.delenv("MAPS_GEMINI_MODEL", raising=False)
    s = Server()
    p, _ = make(s)
    p.search_places("quiet bakery", bbox=BBOX, language="ar")
    req, body = s.requests[0], s.body()
    assert req.method == "POST" and req.headers["x-goog-api-key"] == KEY
    assert KEY not in str(req.url) and req.url.query == b""
    assert body["contents"] == [
        {"role": "user", "parts": [{"text": PROMPT_TEMPLATE.format(text="quiet bakery")}]}
    ]
    assert body["tools"] == [{"googleMaps": {}}]
    assert body["toolConfig"] == {
        "retrievalConfig": {"latLng": {"latitude": 24.5, "longitude": 46.5}, "languageCode": "ar"}
    }
    # against the fixture
    assert set(body) <= set(SCHEMAS["GenerateContentRequest"]["properties"])
    assert "googleMaps" in SCHEMAS["Tool"]["properties"]
    assert set(body["toolConfig"]["retrievalConfig"]) <= set(SCHEMAS["RetrievalConfig"]["properties"])
    assert set(body["toolConfig"]["retrievalConfig"]["latLng"]) == set(SCHEMAS["LatLng"]["properties"])
    assert set(body["contents"][0]) <= set(SCHEMAS["Content"]["properties"])
    assert "text" in SCHEMAS["Part"]["properties"]
    method = FIXTURE["methods"]["models.generateContent"]
    assert method["httpMethod"] == "POST"
    expected = method["path"].replace("{+model}", f"models/{DEFAULT_MODEL}")
    assert req.url.host == "generativelanguage.googleapis.com"
    assert req.url.path == "/" + expected


def test_toolconfig_omitted_without_bbox_or_language_and_partial():
    s = Server()
    p, _ = make(s)
    p.search_places("x")
    assert "toolConfig" not in s.body()
    s2 = Server()
    make(s2)[0].search_places("x", language="en")
    assert s2.body()["toolConfig"] == {"retrievalConfig": {"languageCode": "en"}}
    s3 = Server()
    make(s3)[0].search_places("x", bbox=BBOX)
    assert list(s3.body()["toolConfig"]["retrievalConfig"]) == ["latLng"]


def test_model_in_path_and_no_double_prefix():
    s = Server()
    make(s, model="gemini-x")[0].search_places("x")
    assert s.requests[0].url.path == "/v1beta/models/gemini-x:generateContent"
    s2 = Server()
    make(s2, model="models/gemini-x")[0].search_places("x")
    assert s2.requests[0].url.path == "/v1beta/models/gemini-x:generateContent"


def test_owner_text_is_the_only_variable_content():
    s = Server()
    make(s)[0].search_places("UNIQUE-TEXT")
    prompt = s.body()["contents"][0]["parts"][0]["text"]
    assert prompt == PROMPT_TEMPLATE.replace("{text}", "UNIQUE-TEXT")


# -- response parsing -------------------------------------------------------------------------


def test_response_maps_to_places_notes_attributions_summary():
    payload = response(
        [chunk("P1", "Loaf", "https://m/1", "Quiet sourdough."), chunk("P2", "Crumb", "https://m/2")],
        text="Loaf and Crumb.",
    )
    payload["candidates"][0]["content"]["parts"].append({"text": " More."})
    res = make(Server(payload))[0].search_places("bakery")
    assert [(x.place_id, x.name, x.maps_url) for x in res.places] == [
        ("P1", "Loaf", "https://m/1"),
        ("P2", "Crumb", "https://m/2"),
    ]
    assert res.notes == {"P1": "Quiet sourdough."}
    assert res.attributions == {
        "P1": {"title": "Loaf", "url": "https://m/1"},
        "P2": {"title": "Crumb", "url": "https://m/2"},
    }
    assert res.summary == "Loaf and Crumb. More."
    # the response field names used here exist in the fixture
    assert {"placeId", "title", "uri", "text"} <= set(SCHEMAS["Maps"]["properties"])
    assert "maps" in SCHEMAS["GroundingChunk"]["properties"]
    assert "groundingChunks" in SCHEMAS["GroundingMetadata"]["properties"]
    assert {"content", "groundingMetadata"} <= set(SCHEMAS["Candidate"]["properties"])


def test_chunks_without_maps_or_place_id_are_skipped():
    chunks = [{"web": {"uri": "https://w", "title": "w"}}, chunk(None), chunk("P1"), {}]
    res = make(Server(response(chunks)))[0].search_places("x")
    assert [x.place_id for x in res.places] == ["P1"]


def test_dedup_keeps_order_and_ranks():
    chunks = [chunk("B"), chunk("A"), chunk("B", "dup"), chunk("C")]
    res = make(Server(response(chunks)))[0].search_places("x")
    assert [x.place_id for x in res.places] == ["B", "A", "C"]
    assert [x.hits[0].rank for x in res.places] == [1, 2, 3]
    assert res.places[0].name == "T"


def test_raw_text_truncated():
    res = make(Server(response([chunk("P1")], text="z" * 5000)))[0].search_places("x")
    assert len(res.raw_text) == 2000 and len(res.summary) == 5000


def test_hits():
    p, _ = make(Server(response([chunk("P1")])))
    h = p.search_places("bakery", bbox=BBOX, language="ar").places[0].hits[0]
    assert (h.provider, h.query, h.language, h.cell, h.level, h.rank) == (
        "gemini_maps",
        "bakery",
        "ar",
        BBOX.key(),
        0,
        1,
    )
    h0 = p.search_places("bakery").places[0].hits[0]
    assert h0.cell is None and h0.language is None


# -- budget -----------------------------------------------------------------------------------


def test_free_mode_refuses_before_any_request(grant_spend):
    grant_spend(
        "gemini", "enabled"
    )  # prices are checked, and this SKU's price is unconfirmed: unknown means block
    s = Server()
    p, budget = make(s, budget=Budget(Ledger(":memory:")))
    with pytest.raises(BudgetExceeded):
        p.search_places("x")
    assert s.requests == [] and budget.run_units == {}


def test_charged_once_before_the_request():
    order: list[str] = []
    s = Server()

    class Spy(Budget):
        def charge(self, sku_name, units=1, fingerprint=None):
            order.append(f"charge:{sku_name}:{len(s.requests)}")
            return super().charge(sku_name, units, fingerprint)

    p, budget = make(s, budget=Spy(Ledger(":memory:")))
    p.search_places("x")
    assert order == ["charge:google.gemini_maps_grounding:0"] and len(s.requests) == 1
    assert budget.run_units == {"google.gemini_maps_grounding": 1}


# -- errors -----------------------------------------------------------------------------------


def test_http_500_with_scrubbed_key():
    s = Server({"error": {"code": 500, "message": f"boom {KEY}"}}, status=500)
    with pytest.raises(ProviderError) as e:
        make(s)[0].search_places("x")
    msg = str(e.value)
    assert "500" in msg and "boom" in msg and KEY not in msg and not isinstance(e.value, QuotaExceeded)


def test_http_500_non_json_body_scrubbed():
    p, _ = make(lambda r: httpx.Response(500, text=f"plain {KEY}"))
    with pytest.raises(ProviderError, match="500") as e:
        p.search_places("x")
    assert KEY not in str(e.value)


def test_http_429_is_quota_exceeded():
    s = Server({"error": {"message": f"quota {KEY}"}}, status=429)
    with pytest.raises(QuotaExceeded) as e:
        make(s)[0].search_places("x")
    assert KEY not in str(e.value)


def test_blocked_prompt_names_reason():
    s = Server({"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}})
    assert "PROHIBITED_CONTENT" in SCHEMAS["PromptFeedback"]["properties"]["blockReason"]["enum"]
    with pytest.raises(ProviderError, match="PROHIBITED_CONTENT"):
        make(s)[0].search_places("x")


@pytest.mark.parametrize("payload", [{}, {"candidates": []}])
def test_no_candidates(payload):
    with pytest.raises(ProviderError, match="no candidates"):
        make(Server(payload))[0].search_places("x")


def test_network_error_has_no_key():
    def handler(req):
        raise httpx.ConnectError(f"cannot reach {req.url} with {KEY}")

    with pytest.raises(ProviderError) as e:
        make(handler)[0].search_places("x")
    assert KEY not in str(e.value)


def test_key_never_in_url():
    s = Server()
    make(s)[0].search_places("x", bbox=BBOX, language="en")
    assert KEY not in str(s.requests[0].url)


# -- Provider adapter -------------------------------------------------------------------------


def test_text_search_adapter():
    s = Server(response([chunk(f"P{i}") for i in range(5)]))
    p, budget = make(s)
    page = p.text_search(Query("bakery", "ar"), bbox=BBOX, level=2, max_results=3, tier="ignored")
    assert isinstance(page, SearchPage) and not page.saturated and len(page.places) == 3
    assert page.places[0].hits[0].level == 2 and page.places[0].hits[0].language == "ar"
    assert budget.run_units == {"google.gemini_maps_grounding": 1}


def test_text_search_accepts_string_and_reviews_empty():
    p, _ = make(Server())
    assert len(p.text_search("bakery", bbox=None).places) == 1
    assert p.reviews("P1") == []
