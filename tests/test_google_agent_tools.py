"""Tests for MapsAgentTools (Google Maps MCP server) using httpx.MockTransport (no network)."""

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
from maps.providers.google_agent_tools import DEFAULT_ENDPOINT, AgentSearchResult, MapsAgentTools

FIXTURE = Path(__file__).parent / "fixtures" / "google_agent_tools_tools_list.json"
KEY = "SECRET-KEY-123"
BBOX = BBox(24.0, 46.0, 25.0, 47.0)
PID1, PID2, PID3 = "ChIJaaaaaaaaaa1", "ChIJbbbbbbbbbb2", "ChIJcccccccccc3"

CIRCLE_BIAS = {
    "type": "object",
    "properties": {
        "circle": {
            "type": "object",
            "properties": {
                "center": {
                    "type": "object",
                    "properties": {"latitude": {"type": "number"}, "longitude": {"type": "number"}},
                },
                "radius": {"type": "number"},
            },
        }
    },
}


def schema(**props: Any) -> dict:
    return {"type": "object", "properties": props, "required": ["text_query"]}


DEFAULT_SCHEMA = schema(
    text_query={"type": "string"},
    language_code={"type": "string"},
    location_bias=CIRCLE_BIAS,
)


class Server:
    """Scripted MCP server recording every request."""

    def __init__(self, *, input_schema=None, call_result=None, sse=False, session="sess-1") -> None:
        self.input_schema = DEFAULT_SCHEMA if input_schema is None else input_schema
        self.call_result = call_result if call_result is not None else {"content": [], "isError": False}
        self.sse = sse
        self.session = session
        self.requests: list[httpx.Request] = []
        self.on_call = None  # optional hook run before a tools/call reply
        self.live_listing = False  # reply to tools/list with the captured live response

    def bodies(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests]

    def methods(self) -> list[str]:
        return [b["method"] for b in self.bodies()]

    def calls(self) -> list[dict]:
        return [b for b in self.bodies() if b["method"] == "tools/call"]

    def _reply(self, req_id, result) -> httpx.Response:
        msg = {"jsonrpc": "2.0", "id": req_id, "result": result}
        headers = {"Mcp-Session-Id": self.session} if self.session else {}
        if self.sse:
            body = f"event: message\ndata: {json.dumps(msg)}\n\n"
            return httpx.Response(200, text=body, headers={**headers, "content-type": "text/event-stream"})
        return httpx.Response(200, json=msg, headers=headers)

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        body = json.loads(req.content)
        method = body["method"]
        if method == "initialize":
            return self._reply(body["id"], {"protocolVersion": "2025-06-18", "capabilities": {}})
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list" and self.live_listing:
            live = json.loads(FIXTURE.read_text(encoding="utf-8"))
            return self._reply(body["id"], live["result"])
        if method == "tools/list":
            tool = {"name": "search_places", "inputSchema": self.input_schema}
            other = {"name": "lookup_weather", "inputSchema": {}}
            return self._reply(body["id"], {"tools": [other, tool]})
        if self.on_call:
            return self.on_call(req, body)
        return self._reply(body["id"], self.call_result)


def make(server: Server | Any, *, budget=None, **kw):
    budget = budget or Budget(Ledger(":memory:"))
    client = httpx.Client(transport=httpx.MockTransport(server))
    kw.setdefault("api_key", KEY)
    return MapsAgentTools(budget, client=client, **kw), budget


def structured(*places: dict) -> dict:
    return {"content": [], "structuredContent": {"places": list(places)}}


# -- basics ---------------------------------------------------------------------------------


def test_available_and_protocol(monkeypatch):
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    b = Budget(Ledger(":memory:"))
    assert not MapsAgentTools(b).available()
    assert MapsAgentTools(b, api_key="x").available()
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "y")
    p = MapsAgentTools(b)
    assert p.available() and isinstance(p, Provider) and p.name == "google_agent_tools"


def test_missing_key_errors_before_request_or_charge(monkeypatch):
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    s = Server()
    p, budget = make(s, api_key=None)
    with pytest.raises(ProviderError, match="GOOGLE_MAPS_API_KEY"):
        p.search_places("x")
    assert s.requests == [] and budget.run_units == {}


# -- request sequence and headers -----------------------------------------------------------


def test_request_sequence_headers_and_session_echo():
    s = Server(call_result=structured({"id": PID1}))
    p, _ = make(s)
    p.search_places("quiet bakery")
    assert s.methods() == ["initialize", "notifications/initialized", "tools/list", "tools/call"]
    first = s.requests[0]
    assert str(first.url) == DEFAULT_ENDPOINT and first.method == "POST"
    for r in s.requests:
        assert KEY not in str(r.url)
        assert r.headers["x-goog-api-key"] == KEY
        assert r.headers["content-type"] == "application/json"
        assert r.headers["accept"] == "application/json, text/event-stream"
    assert "mcp-session-id" not in first.headers
    assert all(r.headers["mcp-session-id"] == "sess-1" for r in s.requests[1:])
    bodies = s.bodies()
    assert bodies[0]["jsonrpc"] == "2.0" and bodies[0]["params"]["protocolVersion"]
    assert "id" not in bodies[1]  # a notification has no id
    assert bodies[3]["params"]["name"] == "search_places"


def test_no_session_header_when_server_gives_none():
    s = Server(session="", call_result=structured({"id": PID1}))
    p, _ = make(s)
    p.search_places("x")
    assert all("mcp-session-id" not in r.headers for r in s.requests)


def test_custom_endpoint():
    s = Server()
    p, _ = make(s, endpoint="https://example.test/mcp")
    p.search_places("x")
    assert str(s.requests[0].url) == "https://example.test/mcp"


# -- schema-driven argument mapping ---------------------------------------------------------


@pytest.mark.parametrize("qname", ["text_query", "textQuery", "query", "text"])
def test_query_property_name_variants(qname):
    s = Server(input_schema={"type": "object", "properties": {qname: {"type": "string"}}})
    p, _ = make(s)
    p.search_places("sourdough")
    assert s.calls()[0]["params"]["arguments"] == {qname: "sourdough"}


def test_query_falls_back_to_sole_required_string():
    sch = {
        "type": "object",
        "properties": {"prompt": {"type": "string"}, "limit": {"type": "integer"}},
        "required": ["prompt", "limit"],
    }
    s = Server(input_schema=sch)
    p, _ = make(s)
    p.search_places("sourdough")
    assert s.calls()[0]["params"]["arguments"] == {"prompt": "sourdough"}


def test_no_usable_property_lists_properties_and_does_not_charge():
    sch = {"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "string"}}}
    s = Server(input_schema=sch)
    p, budget = make(s)
    with pytest.raises(ProviderError, match=r"\['a', 'b'\]"):
        p.search_places("x")
    assert s.calls() == [] and budget.run_units == {}


def test_language_and_circle_bias_filled_from_schema():
    s = Server(call_result=structured({"id": PID1}))
    p, _ = make(s)
    p.search_places("bakery", bbox=BBOX, language="ar")
    args = s.calls()[0]["params"]["arguments"]
    assert args["text_query"] == "bakery" and args["language_code"] == "ar"
    circle = args["location_bias"]["circle"]
    assert circle["center"] == {"latitude": 24.5, "longitude": 46.5}
    assert 0 < circle["radius"] <= 50_000


def test_rectangle_only_bias_is_skipped_because_a_circle_is_the_only_bias_shape():
    sch = {
        "type": "object",
        "properties": {
            "textQuery": {"type": "string"},
            "locationBias": {
                "type": "object",
                "properties": {
                    "rectangle": {
                        "type": "object",
                        "properties": {
                            "low": {"type": "object", "properties": {"latitude": {}, "longitude": {}}},
                            "high": {"type": "object", "properties": {"latitude": {}, "longitude": {}}},
                        },
                    }
                },
            },
        },
    }
    s = Server(input_schema=sch)
    p, _ = make(s)
    p.search_places("bakery", bbox=BBOX)
    assert s.calls()[0]["params"]["arguments"] == {"textQuery": "bakery"}


def test_unrecognisable_bias_and_missing_language_property_are_skipped():
    sch = schema(text_query={"type": "string"}, location_bias={"type": "string"})
    s = Server(input_schema=sch)
    p, _ = make(s)
    p.search_places("bakery", bbox=BBOX, language="ar")
    assert s.calls()[0]["params"]["arguments"] == {"text_query": "bakery"}


def test_optional_args_omitted_when_not_given():
    s = Server()
    p, _ = make(s)
    p.search_places("bakery")
    assert s.calls()[0]["params"]["arguments"] == {"text_query": "bakery"}


def test_tool_missing_from_list():
    def handler(req):
        body = json.loads(req.content)
        if body["method"] == "tools/list":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"tools": []}})
        return Server()(req)

    p, _ = make(handler)
    with pytest.raises(ProviderError, match="search_places"):
        p.search_places("x")


def test_schema_cached_and_session_reused_across_searches():
    s = Server(call_result=structured({"id": PID1}))
    p, budget = make(s)
    p.search_places("a")
    p.search_places("b")
    assert s.methods().count("tools/list") == 1 and s.methods().count("initialize") == 1
    assert s.methods().count("tools/call") == 2
    assert budget.run_units == {"google.agent_tools.search": 2}


# -- response framing -----------------------------------------------------------------------


def test_sse_framed_responses_parse():
    s = Server(sse=True, call_result=structured({"id": PID1, "googleMapsLinks": {"placeUrl": "https://x/1"}}))
    p, _ = make(s)
    res = p.search_places("bakery")
    assert [(x.place_id, x.maps_url) for x in res.places] == [(PID1, "https://x/1")]


def test_sse_picks_reply_matching_request_after_notifications():
    def handler(req):
        body = json.loads(req.content)
        if body["method"] != "tools/call":
            return Server()(req)
        note = {"jsonrpc": "2.0", "method": "notifications/progress", "params": {}}
        reply = {"jsonrpc": "2.0", "id": body["id"], "result": structured({"id": PID2})}
        text = f"data: {json.dumps(note)}\n\nid: 7\nevent: message\ndata: {json.dumps(reply)}\n\n"
        return httpx.Response(200, text=text, headers={"content-type": "text/event-stream"})

    p, _ = make(handler)
    assert [x.place_id for x in p.search_places("x").places] == [PID2]


# -- result parsing -------------------------------------------------------------------------


SUMMARY = (
    "Loaf is a quiet sourdough bakery [0]. It opens early [0]. Crumb is a busy cafe. "
    "Rye Room also bakes sourdough on weekends [2]."
)


def view(pid: str, n: int, *, via_resource: bool = False) -> dict:
    out = {
        "location": {"latitude": 24.0 + n / 10, "longitude": 46.0 + n / 10},
        "googleMapsLinks": {"placeUrl": f"https://maps.google.com/?cid={n}", "reviewsUrl": f"https://r/{n}"},
        "attribution": {"title": f"Google Maps {n}", "url": f"https://attr.example/{n}"},
    }
    if via_resource:
        out["place"] = "places/" + pid
    else:
        out["id"] = pid
        out["place"] = "places/" + pid
    return out


def realistic(**extra: Any) -> dict:
    return {
        "places": [view(PID1, 1), view(PID2, 2, via_resource=True), view(PID3, 3)],
        "summary": SUMMARY,
        "contentToken": "tok",
        **extra,
    }


def check_realistic(res: AgentSearchResult) -> None:
    assert [x.place_id for x in res.places] == [PID1, PID2, PID3]
    assert [x.name for x in res.places] == ["", "", ""]  # the service gives no name
    assert [(x.lat, x.lng) for x in res.places] == [(24.1, 46.1), (24.2, 46.2), (24.3, 46.3)]
    assert [x.maps_url for x in res.places] == [f"https://maps.google.com/?cid={n}" for n in (1, 2, 3)]
    assert res.summary == SUMMARY
    assert res.attributions[PID2] == {"title": "Google Maps 2", "url": "https://attr.example/2"}
    assert set(res.attributions) == {PID1, PID2, PID3}
    assert res.notes == {
        PID1: "Loaf is a quiet sourdough bakery [0]. It opens early [0].",
        PID3: "Rye Room also bakes sourdough on weekends [2].",
    }
    assert [x.hits[0].rank for x in res.places] == [1, 2, 3]


def test_structured_content_per_output_schema():
    p, _ = make(Server(call_result={"content": [], "structuredContent": realistic()}))
    check_realistic(p.search_places("bakery"))


def test_attribution_title_never_becomes_the_place_name():
    p, _ = make(Server(call_result=structured(view(PID1, 1))))
    res = p.search_places("bakery")
    assert res.places[0].name == "" and res.attributions[PID1]["title"] == "Google Maps 1"


def test_json_document_in_first_text_item_when_no_structured_content():
    result = {"content": [{"type": "text", "text": json.dumps(realistic())}]}
    p, _ = make(Server(call_result=result))
    check_realistic(p.search_places("bakery"))


def test_structured_content_wins_over_text():
    result = {
        "structuredContent": realistic(),
        "content": [{"type": "text", "text": json.dumps({"places": [view(PID2, 2)], "summary": "other"})}],
    }
    p, _ = make(Server(call_result=result))
    check_realistic(p.search_places("bakery"))


def test_place_id_falls_back_to_resource_name():
    p, _ = make(Server(call_result=structured({"place": "places/" + PID2})))
    assert [x.place_id for x in p.search_places("x").places] == [PID2]


def test_missing_optional_fields_leave_defaults():
    p, _ = make(Server(call_result=structured({"id": PID1})))
    res = p.search_places("x")
    pl = res.places[0]
    assert (pl.lat, pl.lng, pl.maps_url) == (None, None, None)
    assert res.summary == "" and res.attributions == {} and res.notes == {}


def test_citations_index_the_services_list_even_when_an_entry_is_unusable():
    result = structured({"id": "bad"}, {"id": PID2})
    result["structuredContent"]["summary"] = "Second is good [1]."
    p, _ = make(Server(call_result=result))
    assert p.search_places("x").notes == {PID2: "Second is good [1]."}


def test_notes_do_not_confuse_citation_1_with_10():
    summary = "Ten is far [10]. One is near [1]."
    result = structured({"id": PID1}, {"id": PID2})
    result["structuredContent"]["summary"] = summary
    p, _ = make(Server(call_result=result))
    assert p.search_places("x").notes == {PID2: "One is near [1]."}


def test_ids_from_free_text_dedup_and_order():
    text = f"1. Loaf (places/{PID2}) is great. Also {PID1}. Again places/{PID2} and {PID3}; {PID1}."
    p, _ = make(Server(call_result={"content": [{"type": "text", "text": text}]}))
    res = p.search_places("bakery")
    assert [x.place_id for x in res.places] == [PID2, PID1, PID3]
    assert res.raw_text == text


def test_short_ids_and_unrelated_strings_ignored():
    result = structured({"id": "short"}, {"id": "not an id at all!!"})
    p, _ = make(Server(call_result=result))
    assert p.search_places("x").places == []


def test_raw_text_truncated():
    result = {"content": [{"type": "text", "text": "z" * 5000}]}
    p, _ = make(Server(call_result=result))
    assert len(p.search_places("x").raw_text) == 2000


def test_regex_fallback_has_no_summary_or_attribution():
    text = f"places/{PID1}"
    p, _ = make(Server(call_result={"content": [{"type": "text", "text": text}]}))
    res = p.search_places("x")
    assert [x.place_id for x in res.places] == [PID1]
    assert res.summary == "" and res.attributions == {} and res.notes == {}


def test_hits_rank_cell_language_level():
    p, _ = make(Server(call_result=structured({"id": PID1}, {"id": PID2})))
    page = p.text_search(Query("bakery", "en"), bbox=BBOX, level=2)
    h = page.places[1].hits[0]
    assert (h.provider, h.query, h.language, h.cell, h.level, h.rank) == (
        "google_agent_tools",
        "bakery",
        "en",
        BBOX.key(),
        2,
        2,
    )
    res = p.search_places("bakery")
    h0 = res.places[0].hits[0]
    assert h0.cell is None and h0.language is None and h0.rank == 1


# -- budget ---------------------------------------------------------------------------------


def test_charged_once_per_search_not_for_handshake():
    charges: list[tuple[str, list[str]]] = []
    s = Server(call_result=structured({"id": PID1}))

    class Spy(Budget):
        def charge(self, sku_name, units=1):
            charges.append((sku_name, list(s.methods())))
            return super().charge(sku_name, units)

    p, _ = make(s, budget=Spy(Ledger(":memory:")))
    p.search_places("x")
    assert charges == [
        ("google.agent_tools.search", ["initialize", "notifications/initialized", "tools/list"])
    ]


def test_budget_refusal_prevents_the_call():
    class Refuse(Budget):
        def charge(self, sku_name, units=1):
            raise BudgetExceeded("nope")

    s = Server()
    p, _ = make(s, budget=Refuse(Ledger(":memory:")))
    with pytest.raises(BudgetExceeded):
        p.search_places("x")
    assert s.calls() == []


# -- errors ---------------------------------------------------------------------------------


def test_jsonrpc_error_object():
    def on_call(req, body):
        err = {"code": -32602, "message": f"bad key {KEY}"}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "error": err})

    s = Server()
    s.on_call = on_call
    p, _ = make(s)
    with pytest.raises(ProviderError) as e:
        p.search_places("x")
    assert "-32602" in str(e.value) and "bad key" in str(e.value) and KEY not in str(e.value)


def test_is_error_result():
    result = {"isError": True, "content": [{"type": "text", "text": f"API not enabled for {KEY}"}]}
    p, _ = make(Server(call_result=result))
    with pytest.raises(ProviderError) as e:
        p.search_places("x")
    assert "API not enabled" in str(e.value) and KEY not in str(e.value)


def test_http_500_with_key_echo_scrubbed():
    def on_call(req, body):
        return httpx.Response(500, json={"error": {"message": f"boom {KEY}"}})

    s = Server()
    s.on_call = on_call
    p, _ = make(s)
    with pytest.raises(ProviderError) as e:
        p.search_places("x")
    assert "500" in str(e.value) and "boom" in str(e.value) and KEY not in str(e.value)
    assert not isinstance(e.value, QuotaExceeded)


def test_http_500_non_json_body():
    p, _ = make(lambda r: httpx.Response(500, text=f"plain {KEY}"))
    with pytest.raises(ProviderError, match="500") as e:
        p.search_places("x")
    assert KEY not in str(e.value)


def test_http_429_is_quota_exceeded():
    def on_call(req, body):
        return httpx.Response(429, json={"error": {"message": f"quota {KEY}"}})

    s = Server()
    s.on_call = on_call
    p, _ = make(s)
    with pytest.raises(QuotaExceeded) as e:
        p.search_places("x")
    assert KEY not in str(e.value) and isinstance(e.value, ProviderError)


def test_network_error_wrapped_without_key():
    def handler(req):
        raise httpx.ConnectError(f"cannot reach {req.url} with {KEY}")

    p, _ = make(handler)
    with pytest.raises(ProviderError) as e:
        p.search_places("x")
    assert KEY not in str(e.value)


def test_non_json_body_is_provider_error():
    p, _ = make(lambda r: httpx.Response(200, text="<html>nope</html>"))
    with pytest.raises(ProviderError, match="not JSON"):
        p.search_places("x")


# -- Provider adapter -----------------------------------------------------------------------


def test_text_search_adapter_is_unsaturated_and_capped():
    result = structured(*({"id": f"ChIJ{i:012d}"} for i in range(5)))
    p, budget = make(Server(call_result=result))
    page = p.text_search(Query("bakery", "ar"), bbox=BBOX, level=1, max_results=3, tier="ignored")
    assert isinstance(page, SearchPage) and not page.saturated and len(page.places) == 3
    assert page.stopped_early is None and not page.budget_exhausted
    assert budget.run_units == {"google.agent_tools.search": 1}


def test_text_search_accepts_plain_string_and_reviews_empty():
    p, _ = make(Server(call_result=structured({"id": PID1})))
    assert len(p.text_search("bakery", bbox=None).places) == 1
    assert p.reviews(PID1) == []


# -- the live listing (tests/fixtures/google_agent_tools_tools_list.json) -------------------------

QUERY = "quiet bakery known for sourdough in Riyadh"
LIVE_BBOX = BBox(24.5, 46.5, 24.9, 46.9)


def live_server(**kw) -> Server:
    s = Server(session="", **kw)
    s.live_listing = True
    return s


def test_fixture_is_the_live_search_places_listing():
    tool = next(t for t in json.loads(FIXTURE.read_text())["result"]["tools"] if t["name"] == "search_places")
    assert tool["inputSchema"]["required"] == ["textQuery"]
    assert set(tool["inputSchema"]["properties"]) == {
        "textQuery",
        "locationBias",
        "languageCode",
        "regionCode",
        "pageSize",
        "includeExtendedDetails",
    }


def test_live_listing_arguments_are_exactly_the_documented_ones():
    s = live_server(call_result=structured({"id": PID1}))
    p, _ = make(s)
    p.search_places(QUERY, bbox=LIVE_BBOX, language="ar")
    lat, lng = LIVE_BBOX.center
    assert s.calls()[0]["params"]["arguments"] == {
        "textQuery": QUERY,
        "languageCode": "ar",
        "locationBias": {
            "circle": {
                "center": {"latitude": lat, "longitude": lng},
                "radiusMeters": min(LIVE_BBOX.circumradius_m, 50_000),
            }
        },
    }
    assert all("mcp-session-id" not in r.headers for r in s.requests)


def test_live_listing_radius_is_capped_at_50km():
    s = live_server(call_result=structured({"id": PID1}))
    p, _ = make(s)
    p.search_places(QUERY, bbox=BBox(20.0, 40.0, 30.0, 50.0))
    circle = s.calls()[0]["params"]["arguments"]["locationBias"]["circle"]
    assert circle["radiusMeters"] == 50_000


def test_live_listing_query_only():
    s = live_server(call_result=structured({"id": PID1}))
    p, _ = make(s)
    p.search_places(QUERY)
    assert s.calls()[0]["params"]["arguments"] == {"textQuery": QUERY}


# -- free-first page size -------------------------------------------------------------------


@pytest.mark.parametrize(("asked", "sent"), [(1, 1), (3, 3), (5, 5), (0, 1), (-4, 1)])
def test_page_size_clamped_to_1_through_5(asked, sent):
    s = live_server(call_result=structured({"id": PID1}))
    p, _ = make(s)
    p.search_places(QUERY, page_size=asked)
    args = s.calls()[0]["params"]["arguments"]
    assert args["pageSize"] == sent and "includeExtendedDetails" not in args


@pytest.mark.parametrize("asked", [6, 10, 99])
def test_page_size_above_5_is_refused_before_any_request_or_charge(asked):
    s = live_server()
    p, budget = make(s)
    with pytest.raises(ValueError, match="higher tier"):
        p.search_places(QUERY, page_size=asked)
    assert s.requests == [] and budget.run_units == {}


def test_page_size_never_sent_unless_asked_and_extended_details_never():
    s = live_server(call_result=structured({"id": PID1}))
    p, _ = make(s)
    p.search_places(QUERY, bbox=LIVE_BBOX, language="en")
    p.search_places(QUERY, page_size=2)
    for c in s.calls():
        assert "includeExtendedDetails" not in c["params"]["arguments"]
    assert "pageSize" not in s.calls()[0]["params"]["arguments"]


def test_docstring_states_extended_details_is_never_sent():
    doc = MapsAgentTools.search_places.__doc__ or ""
    assert "includeExtendedDetails" in doc and "never" in doc


# -- account protection ---------------------------------------------------------------------


def test_suspension_is_a_policy_stop_but_an_invalid_key_is_an_ordinary_error():
    from maps.providers.base import PolicyStop

    def body(msg):
        return lambda req: httpx.Response(403, json={"error": {"message": msg}})

    p, _ = make(body("Your account has been suspended"))
    with pytest.raises(PolicyStop) as e:
        p.search_places("bakery")
    assert e.value.provider == "google_agent_tools"
    p, _ = make(body("API key not valid. Please pass a valid API key."))
    with pytest.raises(ProviderError) as e2:
        p.search_places("bakery")
    assert not isinstance(e2.value, PolicyStop)


def test_a_personal_oauth_token_is_refused_before_any_request():
    from maps import policy

    s = Server()
    p, _ = make(s, api_key="ya29.token")
    with pytest.raises(policy.PolicyViolation):
        p.search_places("bakery")
    assert s.requests == []
