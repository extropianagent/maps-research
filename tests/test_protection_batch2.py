"""Second batch of account-protection fixes: detection precision, proxies, policy folder, run IDs, gating."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from maps import cli, config, policy
from maps.budget import Budget, Ledger
from maps.geography import BBox
from maps.models import Hit, Place, Query
from maps.providers import http as provider_http
from maps.providers.apify import ApifyProvider
from maps.providers.base import PolicyStop, ProviderError
from maps.providers.dataforseo import DataForSEOProvider
from maps.providers.gemini_maps import GeminiMaps
from maps.providers.google_agent_tools import MapsAgentTools
from maps.providers.google_aggregate import PlacesAggregate
from maps.providers.google_places import GooglePlaces
from maps.store import RunStore

BOX = BBox(24.6, 46.6, 24.7, 46.7)
TOOLS = json.loads((Path(__file__).parent / "fixtures" / "google_agent_tools_tools_list.json").read_text())


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("MAPS_STATE_DIR", str(tmp_path / "state"))
    for name in (
        "HTTPS_PROXY",
        "https_proxy",
        "ALL_PROXY",
        "all_proxy",
        "HTTP_PROXY",
        "http_proxy",
        "MAPS_GEMINI_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)
    return tmp_path / "state"


def budget() -> Budget:
    return Budget(Ledger(":memory:"), mode="quality", max_usd=100.0)


# --- 1. enforcement detection ------------------------------------------------------------------

POSITIVE = [
    "Your account has been suspended.",
    "Account suspension in effect",
    "This account is under a ban",
    "Your account has been terminated for misuse",
    "We've detected unusual activity",
    "Automated queries detected: abuse of the service",
    "Our systems have detected unusual traffic from your computer network. Please solve the CAPTCHA",
    "This request violates our Terms of Service",
    "Access denied for violating the acceptable use policy",
    "Consumer 'api_key:abc' has been suspended",
    "Your account has been disabled by an administrator",
]


@pytest.mark.parametrize("text", POSITIVE)
def test_enforcement_wording_is_detected(text):
    assert policy.detect_enforcement(403, policy.enforcement_text(text))


def test_451_is_enforcement_whatever_the_text():
    assert policy.detect_enforcement(451, "")


BAD_REQUEST = {
    "error": {
        "code": 400,
        "message": "",
        "status": "INVALID_ARGUMENT",
        "details": [
            {
                "@type": "type.googleapis.com/google.rpc.BadRequest",
                "fieldViolations": [
                    {"field": "text_query", "description": "Empty text_query is not allowed"}
                ],
            }
        ],
    }
}
QUOTA_FAILURE = {
    "error": {
        "code": 429,
        "message": "Quota exceeded",
        "details": [
            {
                "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                "violations": [
                    {
                        "subject": "project:1",
                        "description": "Quota exceeded for quota metric 'SearchTextRequest' and limit 'SearchTextRequest per day'",
                    }
                ],
            }
        ],
    }
}
NEGATIVE = [
    "upstream connect error or disconnect/reset before headers. reset reason: connection termination",
    "The billing account for the owning project is disabled in state closed",
    json.dumps(BAD_REQUEST),
    json.dumps(QUOTA_FAILURE),
    "No places found for query 'traffic violation lawyer near me'",
    "API key not valid. Please pass a valid API key.",
    "API key expired. Please renew the API key.",
    "Places API (New) has not been used in project 123 before or it is disabled. "
    "Enable it by visiting https://console.developers.google.com/",
    "Requests to this API places.googleapis.com method google.maps.places.v1.Places.SearchText are blocked.",
    "Requests from referer <empty> are blocked.",
    "This IP, site or mobile application is not authorized to use this API key.",
    "This API method requires billing to be enabled.",
    "Resource has been exhausted (e.g. check quota).",
    "Invalid JSON payload received. Unknown name",
    "The service is currently unavailable.",
    "Deadline exceeded",
    "Place ID is no longer valid",
    "The model is overloaded. Please try again later.",
    "Actor run did not succeed (run ID: x, status: FAILED)",
    "You must rent a paid Actor in order to run it",
    "Payment Required.",
]


@pytest.mark.parametrize("text", NEGATIVE)
def test_ordinary_errors_are_not_enforcement(text):
    assert policy.detect_enforcement(400, policy.enforcement_text(text)) is None


def test_enforcement_text_keeps_values_not_keys_and_recurses_into_json_strings():
    body = {"fieldViolations": "ok", "nested": json.dumps({"violations": "Your account has been suspended"})}
    text = policy.enforcement_text(body)
    assert "fieldViolations" not in text and "Your account has been suspended" in text
    assert policy.enforcement_text(b'{"a": "x"}') == "x"
    assert policy.enforcement_text(None) == ""
    assert len(policy.enforcement_text("a" * 20000)) == 8192


def test_ban_needs_an_account_word_nearby_and_violation_needs_a_rules_word():
    assert policy.detect_enforcement(400, "ban") is None
    assert policy.detect_enforcement(400, "A speeding violation") is None
    assert policy.detect_enforcement(400, "violation of the guideline text")


def _handler_response(status, body, ctype):
    return lambda req: httpx.Response(status, content=body.encode(), headers={"content-type": ctype})


def test_html_429_with_wording_after_300_chars_stops_after_one_request():
    html = (
        "<html><head><title>x</title></head><body>"
        + "<p>padding</p>" * 30
        + "Our systems have detected unusual traffic.</body></html>"
    )
    assert html.index("unusual") > 300
    seen: list[int] = []

    def h(req):
        seen.append(1)
        return httpx.Response(429, text=html, headers={"content-type": "text/html"})

    p = GooglePlaces(
        budget(), api_key="AIzaT", client=httpx.Client(transport=httpx.MockTransport(h)), sleep=lambda s: None
    )
    with pytest.raises(PolicyStop):
        p.details("ChIJabcdefghijk")
    assert len(seen) == 1


def test_plain_503_connection_termination_is_a_provider_error_not_a_stop():
    def h(req):
        return httpx.Response(
            503,
            text="upstream connect error or disconnect/reset before headers. reset reason: connection termination",
        )

    p = GooglePlaces(
        budget(), api_key="AIzaT", client=httpx.Client(transport=httpx.MockTransport(h)), sleep=lambda s: None
    )
    with pytest.raises(ProviderError):
        p.details("ChIJabcdefghijk")


@pytest.mark.parametrize("body", [BAD_REQUEST, QUOTA_FAILURE])
def test_google_error_details_do_not_stop_providers(body):
    def h(req):
        return httpx.Response(400, json=body)

    for build in (
        lambda c: GooglePlaces(budget(), api_key="AIzaT", client=c, sleep=lambda s: None).details(
            "ChIJabcdefghijk"
        ),
        lambda c: GeminiMaps(budget(), api_key="AIzaG", client=c).search_places("x"),
    ):
        with pytest.raises(ProviderError):
            build(httpx.Client(transport=httpx.MockTransport(h)))
    # the aggregate provider reports the failure in its result; it must not stop
    agg = PlacesAggregate(budget(), api_key="AIzaT", client=httpx.Client(transport=httpx.MockTransport(h)))
    assert agg.enumerate(BOX, types=["bakery"]).stopped


def _agent(tool_text):
    def h(req):
        b = json.loads(req.content)
        m = b.get("method")
        if m == "initialize":
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": b["id"], "result": {"protocolVersion": "2025-06-18"}}
            )
        if m == "notifications/initialized":
            return httpx.Response(202)
        if m == "tools/list":
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": b["id"], "result": TOOLS.get("result", TOOLS)}
            )
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": b["id"],
                "result": {"isError": True, "content": [{"type": "text", "text": tool_text}]},
            },
        )

    return MapsAgentTools(budget(), api_key="AIzaT", client=httpx.Client(transport=httpx.MockTransport(h)))


def test_agent_tool_error_echoing_the_query_is_not_a_stop():
    with pytest.raises(ProviderError):
        _agent("No places found for query 'traffic violation lawyer near me'").search_places("x")


def test_agent_tool_error_with_google_error_json_is_not_a_stop():
    with pytest.raises(ProviderError):
        _agent(json.dumps(BAD_REQUEST)).search_places("x")


def test_agent_tool_error_with_real_enforcement_stops():
    with pytest.raises(PolicyStop):
        _agent(json.dumps({"error": {"message": "Your project has been suspended"}})).search_places("x")


def test_dataforseo_and_apify_ordinary_errors_are_not_stops():
    d = DataForSEOProvider(
        budget(),
        login="a",
        password="b",
        client=httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(402, text="Payment Required."))
        ),
        sleep=lambda s: None,
    )
    with pytest.raises(ProviderError):
        d.text_search(Query("x"), bbox=BOX)
    a = ApifyProvider(
        budget(),
        token="t",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    402, json={"error": {"message": "You must rent a paid Actor in order to run it"}}
                )
            )
        ),
    )
    with pytest.raises(ProviderError):
        a.text_search(Query("x"), bbox=BOX)


# --- 2. proxies from the environment ----------------------------------------------------------------

PROVIDER_CLASSES = [
    lambda b: GooglePlaces(b, api_key="AIzaT"),
    lambda b: PlacesAggregate(b, api_key="AIzaT"),
    lambda b: GeminiMaps(b, api_key="AIzaG"),
    lambda b: MapsAgentTools(b, api_key="AIzaT"),
    lambda b: ApifyProvider(b, token="t"),
    lambda b: DataForSEOProvider(b, login="a", password="b"),
]


@pytest.mark.parametrize("build", PROVIDER_CLASSES)
@pytest.mark.parametrize("var", ["HTTPS_PROXY", "all_proxy", "http_proxy"])
def test_residential_proxy_in_the_environment_is_refused_by_every_provider(build, var, monkeypatch):
    monkeypatch.setenv(var, "http://rotating-residential.example:8000")
    with pytest.raises(policy.PolicyViolation, match="egress_proxy"):
        build(budget())


def test_loopback_proxy_is_allowed(monkeypatch):
    for url in ("http://127.0.0.1:41441", "http://localhost:3128", "http://[::1]:8080", "127.0.0.5:80"):
        monkeypatch.setenv("HTTPS_PROXY", url)
        provider_http.make_client(timeout=5).close()


def test_approved_proxy_host_is_allowed_and_needs_a_purpose(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://gateway.corp.example:3128")
    with pytest.raises(policy.PolicyViolation):
        provider_http.make_client(timeout=5)
    with pytest.raises(policy.PolicyViolation, match="purpose"):
        policy.approve("gateway.corp.example", "egress_proxy", owner_statement="ok", details={})
    policy.approve(
        "gateway.corp.example", "egress_proxy", owner_statement="ok", details={"purpose": "office egress"}
    )
    provider_http.make_client(timeout=5).close()


def test_proxy_with_userinfo_is_refused_even_when_approved_or_loopback(monkeypatch):
    policy.approve("gateway.corp.example", "egress_proxy", owner_statement="ok", details={"purpose": "p"})
    for url in ("http://user:pw@gateway.corp.example:3128", "http://user:pw@127.0.0.1:3128"):
        monkeypatch.setenv("HTTPS_PROXY", url)
        with pytest.raises(policy.PolicyViolation, match="credentials"):
            provider_http.make_client(timeout=5)


def test_proxy_refusal_names_the_approve_command(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://px.example:1")
    with pytest.raises(policy.PolicyViolation) as e:
        policy.check_proxy_env()
    msg = str(e.value)
    assert (
        "maps policy approve px.example --kind egress_proxy --owner-statement" in msg and "--purpose" in msg
    )


def test_cli_approve_egress_proxy_takes_purpose():
    r = CliRunner().invoke(
        cli.main,
        [
            "policy",
            "approve",
            "px.example",
            "--kind",
            "egress_proxy",
            "--owner-statement",
            "ok",
            "--purpose",
            "office",
        ],
    )
    assert r.exit_code == 0, r.output
    assert policy.approved("px.example", "egress_proxy")["details"] == {"purpose": "office"}


def test_policy_status_reports_the_proxy_check(monkeypatch):
    assert policy.status()["proxy"]["ok"] is True
    monkeypatch.setenv("HTTPS_PROXY", "http://px.example:1")
    st = policy.status()["proxy"]
    assert st["ok"] is False and "px.example" in st["problem"]
    r = CliRunner().invoke(cli.main, ["doctor"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["account_protection"]["proxy"]["ok"] is False


def test_only_the_http_module_builds_clients():
    root = Path(cli.__file__).parent
    offenders = []
    for path in root.rglob("*.py"):
        if path == root / "providers" / "http.py":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if (
                    node.func.attr in ("Client", "AsyncClient")
                    and getattr(node.func.value, "id", "") == "httpx"
                ):
                    offenders.append(str(path))
    assert offenders == []


# --- 3. policy folder outside a source checkout -------------------------------------------------------


def test_policy_dir_in_a_source_checkout_and_installed(tmp_path, monkeypatch):
    config.set_policy_dir_for_tests(None)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "pyproject.toml").write_text("")
    monkeypatch.setattr(config, "REPO_ROOT", checkout)
    assert config.policy_dir() == checkout / "policy"
    assert config.policy_location() == "source checkout"
    installed = tmp_path / "site-packages"
    installed.mkdir()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(config, "REPO_ROOT", installed)
    assert config.policy_dir() == home / ".maps-research-policy"
    assert config.policy_location() == "per-user folder"
    st = policy.status()["policy_dir"]
    assert st["path"] == str(home / ".maps-research-policy") and st["location"] == "per-user folder"


def test_policy_dir_is_not_under_the_state_dir_or_configurable(tmp_path, monkeypatch):
    config.set_policy_dir_for_tests(None)
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    monkeypatch.setenv("MAPS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("MAPS_POLICY_DIR", str(tmp_path / "elsewhere"))
    assert config.policy_dir() == tmp_path / "h" / ".maps-research-policy"


# --- 4. run IDs are IDs --------------------------------------------------------------------------------


def _sim_run(state):
    r = CliRunner().invoke(cli.main, ["search", "-q", "bakery", "--provider", "sim", "--depth", "quick"])
    assert r.exit_code == 0, r.output
    return json.loads(r.stdout)["run_id"]


@pytest.mark.parametrize("bad", ["ABS", "REL", "20250101-000000-abcd"])
@pytest.mark.parametrize(
    "args",
    [
        ["rank", "{}"],
        ["followups", "{}"],
        ["details", "{}"],
        ["reviews", "integrity", "{}"],
        ["search", "-q", "x", "--provider", "sim", "--resume", "{}"],
        ["discover", "x", "--into", "{}"],
        ["enumerate", "--type", "bakery", "--bbox", "24.6,46.6,24.7,46.7", "--into", "{}"],
    ],
)
def test_bad_run_ids_exit_2_without_echoing_a_path(bad, args, state, tmp_path):
    real = _sim_run(state)
    value = {
        "ABS": str(state / "runs" / real),
        "REL": f"../state/runs/{real}",
        "20250101-000000-abcd": "20250101-000000-abcd",
    }[bad]
    r = CliRunner().invoke(cli.main, [a.format(value) for a in args])
    assert r.exit_code == 2, r.output
    assert str(tmp_path) not in r.output and ".." not in r.output and "runs/" not in r.output


def test_runstore_open_rejects_non_ids(tmp_path):
    run = RunStore.create(tmp_path, {})
    assert RunStore.open(tmp_path, run.run_id).run_id == run.run_id
    for bad in (str(run.dir), f"../{tmp_path.name}/runs/{run.run_id}", "x", ""):
        with pytest.raises(FileNotFoundError) as e:
            RunStore.open(tmp_path, bad)
        assert str(tmp_path) not in str(e.value)


# --- 5. grounded output is gated wherever it is shown --------------------------------------------------


def _grounded_run(provider="google_agent_tools"):
    run = RunStore.create(config.state_dir(), {"queries": [], "providers": [provider], "bbox": None})
    run.record("k", [Place("ChIJgrounded0001", hits=[Hit(provider=provider, query="q")])], saturated=False)
    return run.run_id


@pytest.mark.parametrize("provider", ["google_agent_tools", "gemini_maps"])
@pytest.mark.parametrize(
    "args",
    [
        ["rank", "{}"],
        ["followups", "{}"],
        ["details", "{}", "--missing"],
        ["reviews", "fetch", "{}", "--provider", "sim"],
        ["reviews", "search", "{}", "phrase"],
        ["reviews", "integrity", "{}"],
        ["reviews", "contributors", "{}"],
        ["search", "-q", "x", "--provider", "sim", "--resume", "{}"],
    ],
)
def test_grounded_runs_are_refused_without_an_approved_ai_host(provider, args):
    rid = _grounded_run(provider)
    r = CliRunner().invoke(cli.main, [a.format(rid) for a in args])
    assert r.exit_code == 2 and "Account protection:" in r.output and "ChIJgrounded0001" not in r.output


def test_eval_commands_on_grounded_runs_are_gated(tmp_path):
    rid = _grounded_run()
    f = tmp_path / "e.json"
    f.write_text(json.dumps({"name": "n", "places": [{"place_id": "ChIJgrounded0001"}]}))
    for cmd in ("check", "compare"):
        flag = "--expect" if cmd == "check" else "--baseline"
        r = CliRunner().invoke(cli.main, ["eval", cmd, rid, flag, str(f)])
        assert r.exit_code == 2, (cmd, r.output)


def test_runs_listing_is_exempt_and_approved_host_allows_rank(monkeypatch):
    rid = _grounded_run()
    assert CliRunner().invoke(cli.main, ["runs"]).exit_code == 0
    monkeypatch.setenv("MAPS_AI_HOST", "h1")
    policy.approve("h1", "ai_host", owner_statement="ok", details={"data_handling": "no training"})
    assert CliRunner().invoke(cli.main, ["rank", rid]).exit_code == 0
    policy.revoke("h1")
    assert CliRunner().invoke(cli.main, ["rank", rid]).exit_code == 2


def test_non_grounded_runs_need_no_host():
    run = RunStore.create(config.state_dir(), {"queries": ["x"], "providers": ["sim"]})
    run.record("k", [Place("ChIJplain000001", hits=[Hit(provider="sim", query="q")])], saturated=False)
    assert CliRunner().invoke(cli.main, ["rank", run.run_id]).exit_code == 0


# --- 6. Gemini model name --------------------------------------------------------------------------------


@pytest.mark.parametrize("model", ["gemini-2.5-pro", "gemini-flash-latest", "models/gemini-2.0-flash"])
def test_gemini_model_names_are_accepted(model, monkeypatch):
    monkeypatch.setenv("MAPS_GEMINI_MODEL", model)
    assert GeminiMaps(budget(), api_key="k")._url().endswith(":generateContent")


@pytest.mark.parametrize(
    "model", ["gpt-4o", "gemini-", "gemini-x/../../v1/other", "gemini-a?x=1", "Gemini-pro", "tunedModels/x"]
)
def test_other_model_names_are_refused(model, monkeypatch):
    monkeypatch.setenv("MAPS_GEMINI_MODEL", model)
    seen = []
    c = httpx.Client(transport=httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200, json={})))
    with pytest.raises(policy.PolicyViolation, match="only on Gemini models"):
        GeminiMaps(budget(), api_key="k", client=c).search_places("x")
    assert seen == []


def test_discover_output_names_the_model(monkeypatch):
    class Fake:
        model = "gemini-2.5-pro"

        def available(self):
            return True

        def search_places(self, text, bbox=None, language=None):
            from maps.providers.google_agent_tools import AgentSearchResult

            return AgentSearchResult(places=[], summary="")

    monkeypatch.setenv("MAPS_AI_HOST", "h1")
    policy.approve("h1", "ai_host", owner_statement="ok", details={"data_handling": "no training"})
    monkeypatch.setattr(cli, "build_discoverer", lambda via, budget: Fake())
    r = CliRunner().invoke(cli.main, ["discover", "x", "--via", "gemini"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["model"] == "gemini-2.5-pro"


# --- 7. Google keys -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["1//0gABCDEF", " 1//x", "eyJhbGciOi.x.y", "ya29.x", "Bearer AIza"])
def test_tokens_are_not_keys(key):
    with pytest.raises(policy.PolicyViolation):
        policy.check_google_key(key)


@pytest.mark.parametrize("key", ["AIzaSyABC", "set-by-proxy", "", None])
def test_keys_and_placeholders_pass(key):
    policy.check_google_key(key)


# --- 8. payload scan: proxy only in scheme and host --------------------------------------------------------


def test_maps_url_for_a_place_named_proxy_passes():
    url = "https://www.google.com/maps/place/Proxy+Coffee/@24.71,46.67,17z?q=proxy"
    policy.check_outbound_payload("apify", {"startUrls": [{"url": url}]})


@pytest.mark.parametrize(
    "url",
    [
        "https://proxy.example.com/x",
        "http://rotating-proxy.net:80",
        "socks5://1.2.3.4:1080",
        "http://u:p@host/x",
    ],
)
def test_proxy_hosts_and_schemes_are_still_refused(url):
    with pytest.raises(policy.PolicyViolation):
        policy.check_outbound_payload("apify", {"startUrls": [{"url": url}]})


# --- 9. malformed policy records -----------------------------------------------------------------------------

GOOD = {"data_rights": "a", "retrieval_method": "b", "downstream_use": "c"}


def _write(name, data):
    (config.policy_dir() / name).write_text(json.dumps(data) if not isinstance(data, str) else data)


@pytest.mark.parametrize(
    "records",
    [
        ["apify"],
        [{"subject": "apify", "kind": ["provider"], "owner_statement": "ok", "details": GOOD}],
        [{"subject": ["apify"], "kind": "provider", "owner_statement": "ok", "details": GOOD}],
        [
            {
                "subject": "apify",
                "kind": "provider",
                "owner_statement": "ok",
                "details": {**GOOD, "data_rights": ["a"]},
            }
        ],
        [{"subject": "apify", "kind": "provider", "owner_statement": 5, "details": GOOD}],
        [{"subject": "apify", "kind": "provider", "owner_statement": "ok", "details": "x"}],
    ],
)
def test_malformed_approvals_never_crash_and_approve_nothing(records):
    _write("approvals.json", records)
    with pytest.raises(policy.PolicyViolation):
        policy.require_provider("apify")
    st = policy.status()
    assert st["approvals"] == [] and len(st["invalid_approvals"]) == 1
    r = CliRunner().invoke(cli.main, ["policy", "status"])
    assert r.exit_code == 0, r.output


def test_malformed_stops_are_skipped_and_listed():
    _write("stops.json", ["google", {"provider": "apify", "open": "false"}, {"provider": 3, "open": True}])
    policy.require_provider("sim")
    st = policy.status()
    assert st["open_stops"] == [] and len(st["invalid_stops"]) == 3
    r = CliRunner().invoke(cli.main, ["search", "-q", "x", "--provider", "sim", "--depth", "quick"])
    assert r.exit_code == 0, r.output
    # a valid stop still works next to the junk, and clearing it does not trip over the junk
    _write("stops.json", ["junk", {"provider": "apify", "open": True, "detail": "d"}])
    assert [s["provider"] for s in policy.open_stops()] == ["apify"]
    assert policy.clear_stop("apify", owner_statement="ok") == 1


@pytest.mark.parametrize("content", ["{not json", '{"provider": "apify"}', ""])
def test_unparsable_stops_file_blocks_every_provider(content):
    _write("stops.json", content)
    for name in ("sim", "google", "gemini_maps"):
        with pytest.raises(policy.PolicyViolation, match="stops.json"):
            policy.require_provider(name)
    r = CliRunner().invoke(cli.main, ["search", "-q", "x", "--provider", "sim", "--depth", "quick"])
    assert r.exit_code == 2 and r.stderr.startswith("Account protection:") and "stops.json" in r.stderr
    r = CliRunner().invoke(cli.main, ["policy", "status"])
    assert r.exit_code == 2 and "stops.json" in r.stderr


# --- 10. owner reminders ----------------------------------------------------------------------------------------


def test_owner_responsibilities_include_reading_conditional_terms():
    assert len(policy.OWNER_RESPONSIBILITIES) == 3
    assert policy.OWNER_RESPONSIBILITIES[2] == (
        "Read a conditional provider's own terms before approving it; the record holds what you verified"
    )


def test_regexes_in_this_file_are_sane():
    assert re.compile(r"^\d{8}-\d{6}-[0-9a-f]{4}$").match(RunStore.create(config.state_dir(), {}).run_id)
