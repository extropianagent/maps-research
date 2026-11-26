import asyncio
import json

import pytest

pytest.importorskip("mcp")
from mcp import Client  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from maps import mcp_server  # noqa: E402

EXPECTED_TOOLS = {
    "maps_doctor",
    "maps_budget",
    "maps_area",
    "maps_search",
    "maps_rank",
    "maps_followups",
    "maps_census",
    "maps_details",
    "maps_reviews_fetch",
    "maps_reviews_search",
    "maps_reviews_integrity",
    "maps_reviews_contributors",
    "maps_runs",
    "maps_purge",
}


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("MAPS_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("MAPS_MCP_TOKEN", raising=False)
    for name in ("GOOGLE_MAPS_API_KEY", "APIFY_TOKEN", "DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def with_client(fn):
    async def go():
        async with Client(mcp_server.server) as client:
            return await fn(client)

    return asyncio.run(go())


def call(name, **args):
    """Call a tool over the in-memory transport; returns (is_error, payload)."""

    async def fn(client):
        res = await client.call_tool(name, args)
        text = "".join(getattr(c, "text", "") for c in res.content)
        return res.is_error, text

    is_error, text = with_client(fn)
    return is_error, (text if is_error else json.loads(text))


def ok(name, **args):
    is_error, payload = call(name, **args)
    assert not is_error, payload
    return payload


def sim_search(**extra):
    return ok("maps_search", queries=["bakery"], providers=["sim"], top=5, **extra)


# --- tools ----------------------------------------------------------------------------------


def test_tool_list_has_every_tool_with_cost_text():
    async def fn(client):
        return (await client.list_tools()).tools

    tools = {t.name: t for t in with_client(fn)}
    assert EXPECTED_TOOLS <= set(tools)
    assert "Cost:" in (tools["maps_area"].description or "")
    assert "free" in (tools["maps_rank"].description or "").lower()


def test_doctor_budget_runs_work():
    assert isinstance(ok("maps_doctor"), dict)
    assert isinstance(ok("maps_budget"), dict)
    assert ok("maps_runs")["runs"] == []


def test_search_then_rank_then_followups():
    found = sim_search()
    assert {"run_id", "coverage", "results"} <= set(found)
    assert 0 < len(found["results"]) <= 5
    ranked = ok("maps_rank", run_id=found["run_id"], objective="gems", top=3, min_rating=3.0)
    assert ranked["objective"] == "gems"
    assert len(ranked["results"]) <= 3
    assert isinstance(ok("maps_followups", run_id=found["run_id"]), dict)
    assert [r["run_id"] for r in ok("maps_runs")["runs"]] == [found["run_id"]]


def test_search_filters_are_passed_through():
    found = sim_search(min_rating=4.5, min_reviews=10)
    assert found["filters"]
    assert all(r["rating"] >= 4.5 for r in found["results"])


def test_free_is_the_default_and_max_usd_is_only_sent_with_quality():
    assert mcp_server.search_argv(queries=["a"], providers=["sim"])[-2:] == ["--cost", "free"]
    argv = mcp_server.search_argv(queries=["a"], cost="free", max_usd=5.0)
    assert "--max-usd" not in argv
    argv = mcp_server.search_argv(queries=["a"], cost="quality", max_usd=5.0)
    assert argv[argv.index("--max-usd") + 1] == "5.0"


def test_usage_error_is_a_tool_error_with_the_cli_message():
    is_error, text = call("maps_search", queries=["bakery"], providers=["sim"], cost="quality", max_usd=None)
    assert is_error and text
    is_error, text = call("maps_rank", run_id="nope")
    assert is_error
    assert "no such run: nope" in text
    assert "\n" not in text.strip()


def test_reviews_fetch_then_search_round_trip():
    run_id = sim_search()["run_id"]
    fetched = ok("maps_reviews_fetch", run_id=run_id, provider="sim", top=3)
    assert isinstance(fetched, dict)
    hits = ok("maps_reviews_search", run_id=run_id, query="service", top=5)
    assert isinstance(hits, dict)
    assert isinstance(ok("maps_reviews_integrity", run_id=run_id, top=2), dict)
    assert isinstance(ok("maps_reviews_contributors", run_id=run_id), dict)


def test_purge_all():
    sim_search()
    ok("maps_purge", all=True)
    assert ok("maps_runs")["runs"] == []


def test_instructions_and_resources():
    async def fn(client):
        skill = await client.read_resource("maps://skill")
        agents = await client.read_resource("maps://agents")
        return client.instructions, skill.contents[0].text, agents.contents[0].text

    instructions, skill, agents = with_client(fn)
    assert instructions.count("\n") + 1 <= 12
    assert "skills/maps-research/SKILL.md" in instructions
    assert "Free-first" in agents
    assert skill.strip()


# --- HTTP mode ------------------------------------------------------------------------------


def test_http_mode_refuses_to_start_without_token(capsys):
    with pytest.raises(SystemExit) as exc:
        mcp_server.main(["--http"])
    assert exc.value.code == 2
    err = capsys.readouterr().err.strip()
    assert "MAPS_MCP_TOKEN" in err and "\n" not in err


def test_check_bearer():
    assert mcp_server.check_bearer("Bearer s3cret", "s3cret")
    assert not mcp_server.check_bearer("Bearer wrong", "s3cret")
    assert not mcp_server.check_bearer("s3cret", "s3cret")
    assert not mcp_server.check_bearer(None, "s3cret")
    assert not mcp_server.check_bearer("Bearer s3cret", "")


def test_http_app_enforces_bearer_token():
    app = mcp_server.http_app("s3cret")
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "0"},
        },
    }
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    with TestClient(app) as http:
        assert http.post("/mcp", json=body, headers=headers).status_code == 401
        bad = {**headers, "Authorization": "Bearer nope"}
        assert http.post("/mcp", json=body, headers=bad).status_code == 401
        good = {**headers, "Authorization": "Bearer s3cret"}
        assert http.post("/mcp", json=body, headers=good).status_code == 200


def test_import_error_names_the_extra():
    assert "uv sync --extra mcp" in mcp_server.MISSING_MCP_MESSAGE
