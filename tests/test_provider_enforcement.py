"""Every provider: an enforcement signal stops the run at once, before any retry, sleep or second request."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from maps import policy
from maps.budget import Budget, Ledger
from maps.geography import BBox
from maps.models import Query
from maps.providers.apify import ApifyProvider
from maps.providers.base import PolicyStop, ProviderError, QuotaExceeded
from maps.providers.dataforseo import DataForSEOProvider
from maps.providers.gemini_maps import GeminiMaps
from maps.providers.google_agent_tools import MapsAgentTools
from maps.providers.google_aggregate import PlacesAggregate
from maps.providers.google_places import GooglePlaces

pytestmark = pytest.mark.usefixtures("spending_none")


BOX = BBox(24.6, 46.6, 24.7, 46.7)
PID = "ChIJplaceAAAA1"
TOOLS = json.loads((Path(__file__).parent / "fixtures" / "google_agent_tools_tools_list.json").read_text())
SIGNALS = [
    (429, "Automated queries detected: abuse of the service"),
    (503, "Your project has been suspended for abuse"),
    (403, "We have detected unusual activity on your account"),
]


def budget() -> Budget:
    """Free-mode rules: google, gemini and apify are recorded as billing none (spending_none)."""
    return Budget(Ledger(":memory:"))


def paid_budget() -> Budget:
    """DataForSEO has no free allowance: it is recorded as billing enabled and run in quality mode."""
    return Budget(Ledger(":memory:"), mode="quality", max_usd=100.0, subjects=["dataforseo"])


class Counter:
    """Handler answering every request with one fixed error and counting the requests."""

    def __init__(self, status: int, message: str, *, shape: str = "google") -> None:
        self.status, self.message, self.shape = status, message, shape
        self.requests: list[httpx.Request] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        if self.shape == "text":
            return httpx.Response(self.status, text=self.message)
        return httpx.Response(self.status, json={"error": {"message": self.message}})


def client(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _google(h):
    sleeps: list[float] = []
    p = GooglePlaces(budget(), api_key="AIzaFAKE", client=client(h), sleep=sleeps.append)
    p.sleeps = sleeps  # type: ignore[attr-defined]
    return p


GOOGLE_CALLS = {
    "text_search": lambda p: p.text_search(Query("bakery"), bbox=BOX),
    "details": lambda p: p.details(PID),
    "reviews": lambda p: p.reviews(PID),
    "resolve_area": lambda p: p.resolve_area("Riyadh"),
}


@pytest.mark.parametrize(("status", "message"), SIGNALS)
@pytest.mark.parametrize("method", sorted(GOOGLE_CALLS))
def test_google_places_stops_after_one_request(method, status, message):
    h = Counter(status, message)
    p = _google(h)
    with pytest.raises(PolicyStop):
        GOOGLE_CALLS[method](p)
    assert len(h.requests) == 1 and p.sleeps == []


def test_google_places_a_plain_429_is_still_retried_once():
    h = Counter(429, "Resource has been exhausted (e.g. check quota).")
    p = _google(h)
    with pytest.raises(QuotaExceeded):
        p.details(PID)
    assert len(h.requests) == 2 and p.sleeps == [2]


@pytest.mark.parametrize(("status", "message"), SIGNALS)
@pytest.mark.parametrize("method", ["count", "place_ids", "enumerate"])
def test_aggregate_stops_after_one_request(method, status, message):
    h = Counter(status, message)
    p = PlacesAggregate(budget(), api_key="AIzaFAKE", client=client(h))
    call = {
        "count": lambda: p.count(BOX, types=["bakery"]),
        "place_ids": lambda: p.place_ids(BOX, types=["bakery"]),
        "enumerate": lambda: p.enumerate(BOX, types=["bakery"]),
    }[method]
    with pytest.raises(PolicyStop):
        call()
    assert len(h.requests) == 1


@pytest.mark.parametrize(("status", "message"), SIGNALS)
def test_agent_tools_stops_after_one_request(status, message):
    h = Counter(status, message)
    p = MapsAgentTools(budget(), api_key="AIzaFAKE", client=client(h))
    with pytest.raises(PolicyStop):
        p.search_places("bakery")
    assert len(h.requests) == 1


@pytest.mark.parametrize(("status", "message"), SIGNALS)
def test_gemini_stops_after_one_request(status, message):
    h = Counter(status, message)
    p = GeminiMaps(budget(), api_key="AIzaFAKE", client=client(h))
    with pytest.raises(PolicyStop) as e:
        p.search_places("bakery")
    assert e.value.provider == "gemini_maps" and len(h.requests) == 1


@pytest.mark.parametrize(("status", "message"), SIGNALS)
@pytest.mark.parametrize("method", ["text_search", "reviews", "contributor"])
@pytest.mark.parametrize("shape", ["google", "text"])
def test_apify_stops_after_one_request(method, status, message, shape):
    h = Counter(status, message, shape=shape)
    p = ApifyProvider(budget(), token="tok", client=client(h))
    call = {
        "text_search": lambda: p.text_search(Query("bakery"), bbox=BOX),
        "reviews": lambda: p.reviews(PID),
        "contributor": lambda: p.contributor_reviews("1234567890123", limit=5),
    }[method]
    with pytest.raises(PolicyStop):
        call()
    assert len(h.requests) == 1


def _dfs(h):
    sleeps: list[float] = []
    p = DataForSEOProvider(
        paid_budget(), login="l", password="p", client=client(h), sleep=sleeps.append, poll_interval=1.0
    )
    return p, sleeps


@pytest.mark.parametrize(("status", "message"), SIGNALS)
@pytest.mark.parametrize("method", ["text_search", "reviews"])
def test_dataforseo_stops_after_one_request(method, status, message):
    h = Counter(status, message, shape="text")
    p, sleeps = _dfs(h)
    call = {
        "text_search": lambda: p.text_search(Query("bakery"), bbox=BOX),
        "reviews": lambda: p.reviews(PID),
    }[method]
    with pytest.raises(PolicyStop):
        call()
    assert len(h.requests) == 1 and sleeps == []


def test_dataforseo_enforcement_in_a_poll_stops_without_sleeping_or_polling_again():
    seen: list[httpx.Request] = []

    def h(req):
        seen.append(req)
        if req.method == "POST":
            return httpx.Response(
                200,
                json={
                    "status_code": 20000,
                    "tasks": [{"id": "t1", "status_code": 20100, "status_message": "Task Created."}],
                },
            )
        return httpx.Response(429, text="Automated queries detected: abuse")

    p, sleeps = _dfs(h)
    with pytest.raises(PolicyStop):
        p.reviews(PID)
    assert len(seen) == 2 and sleeps == []


# --- item 10: DataForSEO status messages inside a 200 reply -------------------------------------


def test_dataforseo_top_level_status_message_is_checked_before_malformed():

    def handler(req):
        return httpx.Response(
            200,
            json={
                "status_code": 40104,
                "status_message": "Your account has been suspended for abuse.",
                "tasks": None,
            },
        )

    p, _ = _dfs(handler)
    with pytest.raises(PolicyStop) as e:
        p.text_search(Query("bakery"), bbox=BOX)
    assert e.value.provider == "dataforseo"


def test_dataforseo_task_status_message_is_checked_for_any_code():
    def handler(req):
        task = {"status_code": 40501, "status_message": "Account suspended: terms of service violation"}
        return httpx.Response(200, json={"status_code": 20000, "status_message": "Ok.", "tasks": [task]})

    p, _ = _dfs(handler)
    with pytest.raises(PolicyStop):
        p.text_search(Query("bakery"), bbox=BOX)


def test_dataforseo_ordinary_task_error_is_still_an_ordinary_error():
    def handler(req):
        task = {"status_code": 40501, "status_message": "Invalid Field: 'keyword'."}
        return httpx.Response(200, json={"status_code": 20000, "status_message": "Ok.", "tasks": [task]})

    p, _ = _dfs(handler)
    with pytest.raises(ProviderError) as e:
        p.text_search(Query("bakery"), bbox=BOX)
    assert not isinstance(e.value, PolicyStop)


# --- item 3: Gemini key check and family --------------------------------------------------------


@pytest.mark.parametrize("key", ["ya29.personal", " YA29.x", "Bearer abc"])
def test_gemini_refuses_a_personal_token_before_any_request(key):
    h = Counter(200, "")
    p = GeminiMaps(budget(), api_key=key, client=client(h))
    with pytest.raises(policy.PolicyViolation):
        p.search_places("bakery")
    assert h.requests == []


def test_a_google_stop_blocks_gemini_and_the_reverse():
    policy.record_stop("google", "suspended")
    with pytest.raises(policy.PolicyViolation, match="stopped"):
        policy.require_provider("gemini_maps")
    policy.clear_stop("google", owner_statement="ok")
    policy.record_stop("gemini_maps", "suspended")
    for name in ("google", "google_aggregate", "google_agent_tools", "gemini_maps"):
        with pytest.raises(policy.PolicyViolation, match="stopped"):
            policy.require_provider(name)


# --- item 4: agent tools, enforcement inside a 200 JSON-RPC reply -------------------------------


def _rpc_server(*, fail_method: str | None = None, reply: dict | None = None):
    seen: list[str] = []

    def handler(req):
        body = json.loads(req.content)
        method = body.get("method")
        seen.append(method)
        if method == fail_method:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], **reply})
        if method == "initialize":
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"protocolVersion": "2025-06-18"}}
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": body["id"], "result": TOOLS.get("result", TOOLS)}
            )
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"content": []}})

    handler.seen = seen  # type: ignore[attr-defined]
    return handler


ERR = {"error": {"code": -32000, "message": "Unusual traffic: account suspended"}}
IS_ERR = {
    "result": {
        "isError": True,
        "content": [{"type": "text", "text": "Your project has been suspended for abuse"}],
    }
}


@pytest.mark.parametrize("fail_method", ["initialize", "tools/list", "tools/call"])
def test_agent_tools_jsonrpc_error_with_enforcement_is_a_policy_stop(fail_method):
    h = _rpc_server(fail_method=fail_method, reply=ERR)
    p = MapsAgentTools(budget(), api_key="AIzaFAKE", client=client(h))
    with pytest.raises(PolicyStop) as e:
        p.search_places("bakery")
    assert e.value.provider == "google_agent_tools"
    assert h.seen[-1] == fail_method


def test_agent_tools_is_error_result_with_enforcement_is_a_policy_stop():
    h = _rpc_server(fail_method="tools/call", reply=IS_ERR)
    p = MapsAgentTools(budget(), api_key="AIzaFAKE", client=client(h))
    with pytest.raises(PolicyStop):
        p.search_places("bakery")


def test_agent_tools_ordinary_errors_stay_ordinary():
    plain_err = {"error": {"code": -32602, "message": "Invalid params"}}
    plain_is_err = {"result": {"isError": True, "content": [{"type": "text", "text": "Invalid pageSize"}]}}
    for method, reply in (("initialize", plain_err), ("tools/call", plain_is_err)):
        p = MapsAgentTools(
            budget(), api_key="AIzaFAKE", client=client(_rpc_server(fail_method=method, reply=reply))
        )
        with pytest.raises(ProviderError) as e:
            p.search_places("bakery")
        assert not isinstance(e.value, PolicyStop)
