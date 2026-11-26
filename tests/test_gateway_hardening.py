"""The spending gateway fails closed: exact bounded arithmetic, derived out-of-pocket totals, a store that
is never "zero" by accident, `billing: none` that still limits, every HTTP request charged, limits across
runs, a lock with a deadline, honest provenance of the owner's records.

Mock transports only; the repo's `policy/` folder is never touched (autouse `policy_dir` fixture).
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
from datetime import date
from decimal import Decimal

import httpx
import pytest
from click.testing import CliRunner

from maps import budget as budget_mod
from maps import cli, config, policy
from maps.budget import (
    DEFAULT_DAILY_REQUEST_CAP,
    MAX_DAILY_REQUEST_CAP,
    MAX_UNITS,
    UNKNOWN_PRICE_MONTHLY_UNITS,
    Budget,
    BudgetExceeded,
    GatewayRefusal,
    Ledger,
    LedgerUnavailable,
    current_day,
    current_month,
)
from maps.geography import BBox
from maps.models import Query
from maps.pricing import SKUS
from maps.providers.apify import ApifyProvider
from maps.providers.base import ProviderError
from maps.providers.dataforseo import DataForSEOProvider
from maps.providers.gemini_maps import GeminiMaps
from maps.providers.google_agent_tools import MapsAgentTools
from maps.providers.google_aggregate import PlacesAggregate
from maps.providers.google_places import GooglePlaces

ENT = "google.text_search.enterprise"
BOX = BBox(24.0, 46.0, 25.0, 47.0)


@pytest.fixture
def usage(policy_dir):
    return policy_dir / "usage.json"


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("MAPS_STATE_DIR", str(tmp_path))
    for name in ("GOOGLE_MAPS_API_KEY", "APIFY_TOKEN", "DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD"):
        monkeypatch.delenv(name, raising=False)


def file_budget(**kw):
    return Budget(Ledger(config.ledger_path()), **kw)


def invoke(*args):
    return CliRunner().invoke(cli.main, [str(a) for a in args])


def restored(*subjects):
    """What the owner's `maps budget restore` leaves behind: these subjects are initialised."""
    with Ledger(config.ledger_path()).transaction() as data:
        for subject in subjects:
            data.setdefault("initialized", {}).setdefault(subject, date.today().isoformat())


def store_doc(usage):
    return json.loads(usage.read_text())


def write_store(usage, months=None, **extra):
    marks = {s: date.today().isoformat() for s in policy.SPEND_SUBJECTS}
    doc = {"initialized": marks, "months": months or {}, "reconciliations": []}
    doc.update(extra)
    usage.write_text(json.dumps(doc))


def _store_text(units: str, usd: str) -> str:
    """A usage.json whose counter is written by hand, so a value like 1e16 or NaN can be put in it."""
    row = "{" + f'"units": {units}, "usd": {usd}' + "}"
    month = "{" + f'"{ENT}": {row}' + "}"
    return (
        "{"
        + f'"initialized": {{"google": "{date.today().isoformat()}"}}, "months": '
        + "{"
        + f'"{current_month()}": {month}'
        + "}}"
    )


# --- 1: exact, bounded arithmetic -------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["1e16", "1e308", "-1", "NaN", "Infinity", str(MAX_UNITS + 1)])
def test_an_out_of_range_units_counter_makes_the_store_unusable(spending_enabled, usage, bad):
    usage.write_text(_store_text(bad, "0"))
    with pytest.raises(BudgetExceeded, match="usage.json"):
        file_budget().charge(ENT)
    with pytest.raises(BudgetExceeded, match="usage.json"):
        file_budget(mode="quality", max_usd=1.0).charge(ENT)


@pytest.mark.parametrize("bad", ["1e16", "1e308", "-1"])
def test_an_out_of_range_money_counter_makes_the_store_unusable(spending_enabled, usage, bad):
    usage.write_text(_store_text("5", bad))
    with pytest.raises(BudgetExceeded, match="usage.json"):
        file_budget().charge(ENT)


@pytest.mark.parametrize("units", [1e16, 1e308, float("nan"), float("inf"), -1, True, MAX_UNITS + 1, "5"])
def test_a_units_argument_outside_the_bounds_is_a_value_error(spending_enabled, units):
    with pytest.raises(ValueError):
        file_budget().charge(ENT, units)
    with pytest.raises(ValueError):
        file_budget().check(ENT, units)
    with pytest.raises(ValueError):
        file_budget().quote(ENT, units)
    with pytest.raises(ValueError):
        file_budget().reconcile(ENT, units, owner_statement="console")


def test_the_counters_never_pass_the_hard_maximum(spending_none, usage):
    write_store(usage, {current_month(): {"google.text_search.ids": {"units": MAX_UNITS - 5, "usd": 0}}})
    before = usage.read_text()
    with pytest.raises(BudgetExceeded, match="hard maximum"):
        file_budget().charge("google.text_search.ids", 10)
    assert usage.read_text() == before
    file_budget().charge("google.text_search.ids", 5)  # exactly the maximum is the last allowed
    assert Ledger(usage).units("google.text_search.ids") == MAX_UNITS


def test_quotes_are_exact_decimals(spending_enabled):
    ledger = Ledger(":memory:")
    ledger.add(ENT, 1000, 0)
    q = Budget(ledger).quote(ENT, 3)
    assert isinstance(q.billable_usd, Decimal) and q.billable_usd == Decimal("0.105")
    assert isinstance(q.out_of_pocket_usd, Decimal)


def test_a_fractional_increment_is_never_absorbed(spending_enabled, usage):
    # 0.1 added ten times is exactly 1 (floats give 0.9999999999999999), and costs are exact
    ledger = Ledger(usage)
    for _ in range(10):
        ledger.add("google.aggregate", Decimal("0.1"), 0)
    assert ledger.units("google.aggregate") == 1


def test_reconcile_beyond_the_maximum_is_refused_through_the_cli(policy_dir):
    r = invoke("budget", "reconcile", "--sku", ENT, "--units", "1e16", "--owner-statement", "s")
    assert r.exit_code == 2
    assert not (policy_dir / "usage.json").exists()


def test_a_spend_record_with_a_huge_cap_is_incomplete(policy_dir):
    for field in ("per_search_cap_usd", "period_cap_usd"):
        with pytest.raises(policy.PolicyViolation, match=field):
            policy.approve(
                "google",
                "spend",
                owner_statement="s",
                details={
                    "billing": "enabled",
                    "per_search_cap_usd": 1,
                    "period_cap_usd": 1,
                    "prices_checked_on": date.today().isoformat(),
                    "prices_source": "x",
                    field: 1e12,
                },
            )
    (policy_dir / "approvals.json").write_text(
        json.dumps(
            [
                {
                    "subject": "google",
                    "kind": "spend",
                    "owner_statement": "o",
                    "details": {
                        "billing": "enabled",
                        "per_search_cap_usd": 1,
                        "period_cap_usd": 1e308,
                        "prices_checked_on": date.today().isoformat(),
                        "prices_source": "x",
                    },
                }
            ]
        )
    )
    with pytest.raises(BudgetExceeded, match="incomplete"):
        Budget(Ledger(":memory:")).charge("google.aggregate")


# --- 2: reconciled and pre-existing usage is always priced -------------------------------------------


def test_units_reconciled_before_any_record_count_toward_the_period_cap(policy_dir):
    Budget(Ledger(config.ledger_path())).reconcile(ENT, 3000, owner_statement="console shows 3000")
    policy.approve(
        "google",
        "spend",
        owner_statement="five dollars a month",
        details={
            "billing": "enabled",
            "per_search_cap_usd": 5.0,
            "period_cap_usd": 5.0,
            "prices_checked_on": date.today().isoformat(),
            "prices_source": "price page",
        },
    )
    assert Ledger(config.ledger_path()).units(ENT) == 3000  # approving did not reset the counters
    restored("google")  # the owner's figures were restored for the subject
    with pytest.raises(BudgetExceeded, match="period cap"):
        file_budget(mode="quality", max_usd=5.0).charge(ENT)


def test_the_month_total_is_derived_from_units_not_read_from_a_stored_figure(grant_spend, usage):
    grant_spend("google", "enabled", per_search_cap_usd=5.0, period_cap_usd=5.0)
    write_store(usage, {current_month(): {ENT: {"units": 3000, "usd": 0}}})  # a stored $0 that is wrong
    with pytest.raises(BudgetExceeded, match="period cap"):
        file_budget(mode="quality", max_usd=5.0).charge(ENT)
    report = file_budget().gateway_report()
    assert report["spend"]["google"]["month_out_of_pocket_usd"] == pytest.approx(2000 * 0.035)


def test_units_recorded_under_billing_none_consume_the_allowance_when_billing_is_enabled(grant_spend, usage):
    grant_spend("google", "none")
    Ledger(usage).add(ENT, 1000, 0)
    grant_spend("google", "enabled")
    restored("google")
    with pytest.raises(BudgetExceeded, match="free allowance exhausted"):
        file_budget().charge(ENT)


def test_the_provider_credit_is_still_subtracted(grant_spend, usage):
    grant_spend("apify", "enabled", per_search_cap_usd=50.0, period_cap_usd=1.0)
    write_store(usage, {current_month(): {"apify.place": {"units": 1000, "usd": 0}}})  # $4 of the $5 credit
    file_budget(mode="quality", max_usd=1.0).charge("apify.place", 100)  # $0.40: inside the credit
    with pytest.raises(BudgetExceeded, match="period cap"):
        file_budget(mode="quality", max_usd=50.0).charge(
            "apify.place", 600
        )  # $2.40 more: $0.80 out of pocket


# --- 3: a missing store is not zero usage ------------------------------------------------------------


def test_recording_a_first_enabled_spend_approval_initialises_the_store(grant_spend, usage):
    assert not usage.exists()
    grant_spend("google", "enabled")
    doc = store_doc(usage)
    assert doc["initialized"] == {"google": date.today().isoformat()} and doc["months"] == {}
    Ledger(usage).add(ENT, 7, 0)
    grant_spend("google", "enabled")  # a second approval keeps the counters
    assert Ledger(usage).units(ENT) == 7


def _refused_with_recovery(usage, **kw):
    with pytest.raises(BudgetExceeded) as e:
        file_budget(**kw).charge(ENT)
    msg = str(e.value)
    assert "usage.json" in msg and "maps budget restore" in msg and "console" in msg
    return msg


def test_a_deleted_store_refuses_every_chargeable_request_under_billing_enabled(spending_enabled, usage):
    usage.unlink()
    _refused_with_recovery(usage)
    _refused_with_recovery(usage, mode="quality", max_usd=1.0)
    assert not usage.exists()  # a refusal creates nothing


@pytest.mark.parametrize("content", ["{}", "", '{"months": {}}', '{"months": {}, "reconciliations": []}'])
def test_an_empty_or_uninitialised_store_refuses_under_billing_enabled(spending_enabled, usage, content):
    usage.write_text(content)
    _refused_with_recovery(usage)


def test_a_second_checkout_with_approvals_but_no_usage_file_is_refused(spending_enabled, usage, tmp_path):
    other = tmp_path / "other-checkout"
    other.mkdir()
    (other / "approvals.json").write_text((usage.parent / "approvals.json").read_text())
    config.set_policy_dir_for_tests(other)
    _refused_with_recovery(other / "usage.json")


def test_a_deleted_store_is_simply_recreated_under_billing_none(spending_none, usage):
    usage.unlink()
    file_budget().charge(ENT)
    assert store_doc(usage)["initialized"] and Ledger(usage).units(ENT) == 1


def test_the_preflight_refuses_too(spending_enabled, usage):
    usage.unlink()
    with pytest.raises(GatewayRefusal, match="maps budget restore"):
        file_budget().preflight("google")


def test_restore_needs_a_figure_for_every_sku_with_a_finite_free_allowance(spending_enabled, usage):
    usage.unlink()
    r = invoke("budget", "restore", "--owner-statement", "console figures", "--subject", "google")
    assert r.exit_code == 2 and ENT in r.stderr and not usage.exists()
    finite = [
        n
        for n, s in SKUS.items()
        if s.provider == "google" and n != "google.gemini_maps_grounding" and s.free_units != float("inf")
    ]
    args = ["budget", "restore", "--owner-statement", "console figures", "--subject", "google"]
    for n in finite:
        args += ["--sku", f"{n}={500 if n == ENT else 0}"]
    r = invoke(*args)
    assert r.exit_code == 0, r.output
    assert "google" in store_doc(usage)["initialized"] and Ledger(usage).units(ENT) == 500
    file_budget().charge(ENT)  # usable again
    assert Ledger(usage).units(ENT) == 501


def test_restore_needs_the_owners_statement_and_validates_figures(spending_enabled, usage):
    usage.unlink()
    assert invoke("budget", "restore", "--sku", f"{ENT}=5").exit_code == 2
    assert invoke("budget", "restore", "--owner-statement", "s", "--sku", ENT).exit_code == 2
    assert invoke("budget", "restore", "--owner-statement", "s", "--sku", f"{ENT}=1e16").exit_code == 2
    assert invoke("budget", "restore", "--owner-statement", "s", "--sku", f"{ENT}=-1").exit_code == 2
    assert not usage.exists()


def test_the_mcp_server_has_no_restore_tool():
    pytest.importorskip("mcp")
    from maps import mcp_server

    assert not any("restore" in n for n in dir(mcp_server) if n.startswith("maps_"))


# --- 4: billing none does not switch off the limits ----------------------------------------------------


def test_billing_none_still_stops_at_the_free_allowance(spending_none, usage):
    Ledger(usage).add(ENT, 51_000, 0)
    with pytest.raises(BudgetExceeded):
        file_budget().charge(ENT)
    Ledger(usage).add("google.text_search.pro", 4_500, 0)
    with pytest.raises(BudgetExceeded, match="90%"):
        file_budget().charge("google.text_search.pro")


def test_billing_none_stops_at_ninety_percent_in_a_run(spending_none, usage):
    Ledger(usage).add(ENT, 899, 0)
    file_budget().charge(ENT)
    with pytest.raises(BudgetExceeded, match="90%"):
        file_budget().charge(ENT)


def test_billing_none_is_never_out_of_pocket(spending_none):
    with pytest.raises(BudgetExceeded, match="free allowance exhausted"):
        Budget(Ledger(":memory:")).charge(
            "dataforseo.maps_search"
        )  # no allowance, priced: it would cost money


def test_billing_none_refuses_cost_quality(spending_none):
    with pytest.raises(GatewayRefusal, match="not chargeable"):
        Budget(Ledger(":memory:"), mode="quality", max_usd=5.0, subjects=["google"])
    r = invoke(
        "search", "-q", "bakery", "--bbox", "24.5,46.5,24.9,46.9", "--cost", "quality", "--max-usd", "1"
    )
    # the provider list is google by default: refused before anything is sent
    assert r.exit_code == 2 and "not chargeable" in r.stderr


def test_a_quality_run_is_refused_at_the_charge_for_a_none_subject(spending_none):
    b = Budget(Ledger(":memory:"), mode="quality", max_usd=5.0, subjects=[])
    with pytest.raises(BudgetExceeded, match="not chargeable"):
        b.charge("google.aggregate")


def test_a_price_unknown_sku_under_none_has_a_monthly_unit_ceiling(spending_none, usage):
    assert UNKNOWN_PRICE_MONTHLY_UNITS == 1000
    sku = "google.agent_tools.search"
    assert not SKUS[sku].price_known
    Ledger(usage).add(sku, 999, 0)
    file_budget().charge(sku)  # the 1,000th
    before = usage.read_text()
    with pytest.raises(BudgetExceeded, match="1,000"):
        file_budget().charge(sku)
    assert usage.read_text() == before
    Ledger(usage).add("google.gemini_maps_grounding", 51_000 - 0, 0)
    with pytest.raises(BudgetExceeded, match="1,000"):
        file_budget().charge("google.gemini_maps_grounding")


def test_price_unknown_stays_blocked_under_enabled(grant_spend):
    grant_spend("google", "enabled")
    with pytest.raises(BudgetExceeded, match="unknown price means block"):
        Budget(Ledger(":memory:")).charge("google.agent_tools.search")


# --- 5: every HTTP request goes through the gateway ----------------------------------------------------


def test_the_free_setup_skus_exist():
    for name, provider in (("google.agent_tools.setup", "google"), ("dataforseo.poll", "dataforseo")):
        sku = SKUS[name]
        assert sku.provider == provider and sku.usd_per_unit == 0 and sku.free_units == float("inf")
        assert sku.price_known


class Trace(Budget):
    """Logs one "C" for every successful charge, wherever the provider calls it from."""

    def __init__(self, *a, log, **kw):
        super().__init__(*a, **kw)
        self.log = log

    def charge_many(self, items, fingerprint=None):
        q = super().charge_many(items, fingerprint)
        self.log.append("C")
        return q


def _handler(log, polls):
    def handler(req: httpx.Request) -> httpx.Response:
        log.append("H")
        u = str(req.url)
        body = json.loads(req.content) if req.content else None
        if "places:searchText" in u:
            tok = (body or {}).get("pageToken")
            d = {
                "places": [
                    {
                        "id": f"ChIJ{tok}{i:08d}",
                        "viewport": {
                            "low": {"latitude": 1, "longitude": 1},
                            "high": {"latitude": 2, "longitude": 2},
                        },
                    }
                    for i in range(20)
                ]
            }
            nxt = {None: "p2", "p2": "p3"}.get(tok)
            if nxt:
                d["nextPageToken"] = nxt
            return httpx.Response(200, json=d)
        if "places.googleapis.com/v1/places/" in u:
            return httpx.Response(200, json={"id": "x", "reviews": []})
        if "areainsights" in u:
            if body["insights"] == ["INSIGHT_COUNT"]:
                polls["agg"] = polls.get("agg", 0) + 1
                return httpx.Response(200, json={"count": "150" if polls["agg"] == 1 else "10"})
            return httpx.Response(200, json={"placeInsights": [{"place": "places/ChIJabcdefghij"}]})
        if "mapstools" in u:
            m = body.get("method")
            if m == "initialize":
                return httpx.Response(
                    200,
                    json={"jsonrpc": "2.0", "id": body["id"], "result": {"protocolVersion": "2025-06-18"}},
                )
            if m == "notifications/initialized":
                return httpx.Response(202)
            if m == "tools/list":
                tool = {
                    "name": "search_places",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"textQuery": {"type": "string"}},
                        "required": ["textQuery"],
                    },
                }
                return httpx.Response(
                    200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"tools": [tool]}}
                )
            result = {"structuredContent": {"places": [{"id": "ChIJabcdefghij"}], "summary": ""}}
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})
        if "generativelanguage" in u:
            return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "x"}]}}]})
        if "apify" in u:
            return httpx.Response(201, json=[])
        if "dataforseo" in u:
            if "task_get" in u:
                polls["n"] = polls.get("n", 0) + 1
                if polls["n"] < 3:
                    return httpx.Response(200, json={"tasks": [{"status_code": 20100, "id": "T1"}]})
                return httpx.Response(
                    200, json={"tasks": [{"status_code": 20000, "id": "T1", "result": [{"items": []}]}]}
                )
            if "task_post" in u:
                return httpx.Response(200, json={"tasks": [{"status_code": 20100, "id": "T1"}]})
            return httpx.Response(200, json={"tasks": [{"status_code": 20000, "result": [{"items": []}]}]})
        return httpx.Response(404)

    return handler


def _provider_calls(free_budget, paid_budget, log, polls):
    def c():
        return httpx.Client(transport=httpx.MockTransport(_handler(log, polls)))

    q = Query("bakery", "en")
    g = lambda: GooglePlaces(free_budget, api_key="AIzaK", client=c(), sleep=lambda s: None)  # noqa: E731
    agg = lambda: PlacesAggregate(free_budget, api_key="AIzaK", client=c())  # noqa: E731
    ap = lambda: ApifyProvider(free_budget, token="t", client=c())  # noqa: E731
    dfs = lambda: DataForSEOProvider(  # noqa: E731
        paid_budget, login="l", password="p", client=c(), sleep=lambda s: None
    )
    return {
        "google.text_search": lambda: g().text_search(q, bbox=BOX),
        "google.details": lambda: g().details("ChIJabcdefghij"),
        "google.reviews": lambda: g().reviews("ChIJabcdefghij"),
        "google.resolve_area": lambda: g().resolve_area("Riyadh"),
        "aggregate.count": lambda: agg().count(BOX, types=["bakery"]),
        "aggregate.place_ids": lambda: agg().place_ids(BOX, types=["bakery"]),
        "aggregate.enumerate": lambda: (
            polls.update(agg=0),
            agg().enumerate(BOX, types=["bakery"], max_level=1),
        ),
        "agent_tools.search_places": lambda: MapsAgentTools(
            free_budget, api_key="AIzaK", client=c()
        ).search_places("bakery"),
        "gemini.search_places": lambda: GeminiMaps(free_budget, api_key="AIzaK", client=c()).search_places(
            "bakery"
        ),
        "apify.text_search": lambda: ap().text_search(q, bbox=BOX, max_results=60),
        "apify.reviews": lambda: ap().reviews("ChIJabcdefghij", limit=5),
        "apify.contributor": lambda: ap().contributor_reviews("123456789012", limit=10),
        "dataforseo.text_search": lambda: dfs().text_search(q, bbox=BOX),
        "dataforseo.reviews": lambda: (polls.update(n=0), dfs().reviews("ChIJabcdefghij", limit=5)),
    }


def test_every_provider_method_charges_once_before_every_http_request(grant_spend, usage):
    # google, gemini and apify: billing none (free mode, inside the allowances and the apify credit);
    # dataforseo: enabled, quality, generous caps.
    grant_spend("google", "none")
    grant_spend("gemini", "none")
    grant_spend("apify", "none")  # apify.contributor has an unknown price: allowed only under none
    grant_spend("dataforseo", "enabled")
    log: list[str] = []
    polls: dict = {}
    free = Trace(Ledger(usage), log=log, max_requests=5000)
    paid = Trace(
        Ledger(usage), log=log, mode="quality", max_usd=900.0, max_requests=5000, subjects=["dataforseo"]
    )
    for name, fn in _provider_calls(free, paid, log, polls).items():
        log.clear()
        fn()
        assert log.count("H") >= 1, name
        assert log.count("C") == log.count("H"), f"{name}: {log}"
        pending = False
        for e in log:
            if e == "C":
                pending = True
            else:
                assert pending, f"{name}: an HTTP request left without a charge before it: {log}"
                pending = False


def test_a_retried_request_is_charged_again_and_never_sent_uncharged(grant_spend, usage):
    grant_spend("google", "none")
    log: list[str] = []
    replies = iter([httpx.Response(503, json={"error": {"message": "x"}}), httpx.Response(200, json={})])

    def handler(req):
        log.append("H")
        return next(replies)

    c = httpx.Client(transport=httpx.MockTransport(handler))
    b = Trace(Ledger(usage), log=log)
    GooglePlaces(b, api_key="AIzaK", client=c, sleep=lambda s: None).details("ChIJabcdefghij", tier="ids")
    assert log == ["C", "H", "C", "H"]


def test_the_agent_tools_setup_requests_are_charged_and_counted(spending_none):
    log: list[str] = []
    b = Trace(Ledger(":memory:"), log=log)
    c = httpx.Client(transport=httpx.MockTransport(_handler(log, {})))
    MapsAgentTools(b, api_key="AIzaK", client=c).search_places("bakery")
    assert log.count("H") == 4 and b.requests == 4  # initialize, initialized, tools/list, tools/call
    assert b.run_units["google.agent_tools.setup"] == 3 and b.run_units["google.agent_tools.search"] == 1


def test_the_kill_switch_stops_the_agent_tools_setup_midway(spending_none):
    sent: list[str] = []

    def handler(req):
        body = json.loads(req.content)
        sent.append(body["method"])
        policy.kill("pulled during initialize")
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {}})

    c = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(BudgetExceeded, match="kill switch"):
        MapsAgentTools(Budget(Ledger(":memory:")), api_key="AIzaK", client=c).search_places("bakery")
    assert sent == ["initialize"]


def test_the_setup_requests_count_toward_max_requests(spending_none):
    log: list[str] = []
    b = Budget(Ledger(":memory:"), max_requests=2)
    c = httpx.Client(transport=httpx.MockTransport(_handler(log, {})))
    with pytest.raises(BudgetExceeded, match="max_requests"):
        MapsAgentTools(b, api_key="AIzaK", client=c).search_places("bakery")
    assert log.count("H") == 2


def _dfs(handler, budget, clock, **kw):
    now = clock

    def sleep(s):
        now[0] += s

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return DataForSEOProvider(
        budget,
        login="l",
        password="p",
        client=client,
        sleep=sleep,
        clock=lambda: now[0],
        **kw,
    )


def _dfs_handler(ready_after):
    state = {"gets": 0}

    def handler(req):
        if req.method == "POST":
            return httpx.Response(200, json={"tasks": [{"status_code": 20100, "id": "T1"}]})
        state["gets"] += 1
        if state["gets"] < ready_after:
            return httpx.Response(200, json={"tasks": [{"status_code": 40602}]})
        return httpx.Response(200, json={"tasks": [{"status_code": 20000, "result": [{"items": []}]}]})

    handler.state = state  # type: ignore[attr-defined]
    return handler


def test_every_poll_is_charged_and_polls_are_not_repeats(grant_spend):
    grant_spend("dataforseo", "enabled")
    h = _dfs_handler(ready_after=7)  # more than the three identical requests the repeat rule allows
    b = Budget(Ledger(":memory:"), mode="quality", max_usd=5.0)
    p = _dfs(h, b, [0.0], poll_timeout=600, poll_interval=1)
    p.reviews("ChIJa")
    assert h.state["gets"] == 7 and b.run_units["dataforseo.poll"] == 7
    assert b.requests == 1 + 7  # the post is one request, each poll one more


def test_the_poll_loop_is_bounded_by_wall_clock_not_by_counting_sleeps(grant_spend):
    grant_spend("dataforseo", "enabled")
    h = _dfs_handler(ready_after=10**6)
    now = [0.0]
    b = Budget(Ledger(":memory:"), mode="quality", max_usd=5.0, clock=lambda: now[0])
    p = _dfs(h, b, now, poll_timeout=10, poll_interval=5)
    with pytest.raises(ProviderError, match="not ready before timeout") as e:
        p.reviews("ChIJa")
    assert "T1" in str(e.value)
    assert h.state["gets"] == 2  # t=0 and t=5; at t=10 the deadline is reached


def test_the_poll_deadline_is_the_smaller_of_its_timeout_and_the_budgets_remaining_time(grant_spend):
    grant_spend("dataforseo", "enabled")
    h = _dfs_handler(ready_after=10**6)
    now = [0.0]
    b = Budget(Ledger(":memory:"), mode="quality", max_usd=5.0, clock=lambda: now[0], max_elapsed_s=12)
    p = _dfs(h, b, now, poll_timeout=600, poll_interval=5)
    with pytest.raises(ProviderError, match="not ready before timeout"):
        p.reviews("ChIJa")
    assert h.state["gets"] == 3  # t=0, 5, 10; t=15 is past the 12 s the budget has left


def test_a_kill_switch_stops_the_poll_loop(grant_spend):
    grant_spend("dataforseo", "enabled")

    def handler(req):
        if req.method == "POST":
            return httpx.Response(200, json={"tasks": [{"status_code": 20100, "id": "T1"}]})
        policy.kill("stop polling")
        return httpx.Response(200, json={"tasks": [{"status_code": 40602}]})

    b = Budget(Ledger(":memory:"), mode="quality", max_usd=5.0)
    with pytest.raises(BudgetExceeded, match="kill switch"):
        _dfs(handler, b, [0.0]).reviews("ChIJa")


# --- 6: limits across runs ----------------------------------------------------------------------------


def test_the_daily_request_cap_stops_many_short_runs(grant_spend, usage):
    grant_spend("google", "none", daily_request_cap=20)
    done = 0
    refused = None
    for i in range(25):
        b = file_budget()
        try:
            b.charge("google.text_search.ids", fingerprint=str(i))
            done += 1
        except BudgetExceeded as e:
            refused = e
        finally:
            b.close()
    assert done == 20 and refused is not None and "daily" in str(refused)
    assert store_doc(usage)["days"][current_day()]["google"] == 20


def test_the_default_daily_cap_is_2000_and_the_maximum_20000(grant_spend, usage):
    assert (DEFAULT_DAILY_REQUEST_CAP, MAX_DAILY_REQUEST_CAP) == (2000, 20000)
    grant_spend("google", "none")
    file_budget().charge("google.text_search.ids")  # the first use creates the store
    doc = store_doc(usage)
    doc["days"] = {current_day(): {"google": 1999}}
    usage.write_text(json.dumps(doc))
    file_budget().charge("google.text_search.ids")
    with pytest.raises(BudgetExceeded, match="daily"):
        file_budget().charge("google.text_search.ids")


def test_the_daily_cap_is_per_subject_and_per_day(grant_spend, usage):
    grant_spend("google", "none", daily_request_cap=1)
    grant_spend("dataforseo", "none", daily_request_cap=1)
    file_budget().charge("google.text_search.ids")
    file_budget().charge("dataforseo.poll")  # another subject has its own count
    with pytest.raises(BudgetExceeded, match="daily"):
        file_budget().charge("google.text_search.ids")
    doc = store_doc(usage)
    doc["days"] = {"2000-01-01": {"google": 1}}  # yesterday's count does not carry over
    usage.write_text(json.dumps(doc))
    file_budget().charge("google.text_search.ids")


@pytest.mark.parametrize("bad", [0, -1, 20001, 1.5, "20", True])
def test_the_daily_cap_in_a_spend_record_is_bounded(bad):
    with pytest.raises(policy.PolicyViolation, match="daily_request_cap"):
        policy.approve(
            "google", "spend", owner_statement="s", details={"billing": "none", "daily_request_cap": bad}
        )


def test_the_daily_cap_is_recorded_through_the_cli(policy_dir):
    r = invoke(
        "policy", "approve", "google", "--kind", "spend", "--owner-statement", "s", "--billing", "none",
        "--daily-request-cap", "50",
    )  # fmt: skip
    assert r.exit_code == 0, r.output
    assert policy.spend_record("google")["details"] == {"billing": "none", "daily_request_cap": 50}
    for bad in ("0", "20001"):
        r = invoke(
            "policy", "approve", "google", "--kind", "spend", "--owner-statement", "s", "--billing", "none",
            "--daily-request-cap", bad,
        )  # fmt: skip
        assert r.exit_code == 2
    assert "--daily-request-cap" in invoke("policy", "approve", "--help").stdout


def test_a_third_concurrent_run_is_refused(spending_none, usage):
    a, b, c = file_budget(), file_budget(), file_budget()
    a.charge("google.text_search.ids")
    b.charge("google.text_search.ids")
    before = usage.read_text()
    with pytest.raises(BudgetExceeded, match="2 runs"):
        c.charge("google.text_search.ids")
    assert usage.read_text() == before
    a.charge("google.text_search.ids")  # a run that holds a lease keeps working
    a.close()
    c.charge("google.text_search.ids")  # a lease freed by a finished run makes room
    b.close()
    c.close()
    assert store_doc(usage)["leases"] == []


def test_a_finished_budget_that_is_dropped_frees_its_lease(spending_none, usage):
    for _ in range(5):
        file_budget().charge("google.text_search.ids")  # each temporary run ends at once
    assert store_doc(usage)["leases"] == []


def test_a_stale_lease_does_not_block(spending_none, usage):
    t = [1000.0]
    a = file_budget(wall_clock=lambda: t[0], max_elapsed_s=900)
    b = file_budget(wall_clock=lambda: t[0], max_elapsed_s=900)
    a.charge("google.text_search.ids")
    b.charge("google.text_search.ids")
    leases = store_doc(usage)["leases"]
    assert len(leases) == 2 and {"pid", "host", "started"} <= set(leases[0])
    t[0] = 1000.0 + 900 + 300 + 59
    with pytest.raises(BudgetExceeded, match="2 runs"):
        file_budget(wall_clock=lambda: t[0]).charge("google.text_search.ids")
    t[0] = 1000.0 + 900 + 300 + 61
    c = file_budget(wall_clock=lambda: t[0])
    c.charge("google.text_search.ids")  # both leases are older than max_elapsed_s + 300 s + 60 s
    assert len(store_doc(usage)["leases"]) == 1
    for x in (a, b, c):
        x.close()


def test_the_simulator_takes_no_lease_and_no_daily_count(usage):
    b = file_budget()
    b.charge("sim.search")
    assert not usage.exists()


def test_the_cli_releases_its_lease_when_the_command_ends(
    spending_none, usage, monkeypatch, grant_service_use, ai_host
):
    grant_service_use("google_aggregate")
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "AIzaK")
    from maps.providers import google_aggregate

    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"count": "3"})))
    monkeypatch.setattr(google_aggregate, "make_client", lambda timeout: client)
    kept = []
    real = cli.make_budget
    monkeypatch.setattr(cli, "make_budget", lambda *a, **k: kept.append(real(*a, **k)) or kept[-1])
    for _ in range(4):  # the budgets stay referenced: only the command's end can release the lease
        r = invoke("census", "--bbox", "24.5,46.5,24.9,46.9", "--type", "bakery")
        assert r.exit_code == 0, r.output
        assert store_doc(usage)["leases"] == []
    assert len(kept) == 4


# --- 7: Apify reviews reserve the place too ------------------------------------------------------------


def test_apify_reviews_reserve_the_place_and_the_reviews(spending_enabled, usage):
    sent: list[httpx.Request] = []

    def handler(req):
        sent.append(req)
        return httpx.Response(200, json=[])

    c = httpx.Client(transport=httpx.MockTransport(handler))
    b = file_budget(mode="quality", max_usd=5.0)
    ApifyProvider(b, token="t", client=c).reviews("ChIJabcdefghij", limit=5)
    assert len(sent) == 1
    assert b.run_units == {"apify.place": 1, "apify.review": 5} and b.requests == 1
    assert "unconfirmed" in ApifyProvider.reviews.__doc__


def test_an_apify_reviews_request_that_cannot_reserve_both_sends_nothing_and_records_nothing(
    grant_spend, usage
):
    grant_spend("apify", "enabled", per_search_cap_usd=50.0, period_cap_usd=0.005)
    Ledger(usage).add("apify.place", 1250, 0)  # exactly the $5 credit: the next dollars are out of pocket
    sent: list[httpx.Request] = []
    c = httpx.Client(transport=httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(200, json=[])))
    before = usage.read_text()
    b = file_budget(mode="quality", max_usd=50.0)
    with pytest.raises(BudgetExceeded, match="period cap"):  # the place ($0.004) fits, the reviews do not
        ApifyProvider(b, token="t", client=c).reviews("ChIJabcdefghij", limit=5)
    assert sent == [] and usage.read_text() == before


# --- 8: the store lock has a deadline -----------------------------------------------------------------


def test_a_held_lock_refuses_after_the_deadline_without_waiting_for_real(spending_none, usage):
    now = [0.0]
    naps: list[float] = []

    def nap(s):
        naps.append(s)
        now[0] += s

    fd = os.open(f"{usage}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        ledger = Ledger(usage, lock_timeout=10.0, sleep=nap, clock=lambda: now[0])
        before = usage.read_text()
        with pytest.raises(LedgerUnavailable, match="ledger unavailable"):
            Budget(ledger).charge("google.text_search.ids")
        assert 10.0 <= now[0] < 11.0 and naps
        assert usage.read_text() == before
    finally:
        os.close(fd)


def test_the_default_deadline_is_ten_seconds():
    assert budget_mod.LOCK_TIMEOUT_S == 10.0


def test_a_lock_that_frees_up_in_time_is_taken(spending_none, usage):
    now = [0.0]
    fd = os.open(f"{usage}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)

    def nap(s):
        now[0] += s
        if now[0] >= 1.0:
            os.close(fd)  # closing the descriptor releases the lock

    Budget(Ledger(usage, sleep=nap, clock=lambda: now[0])).charge("google.text_search.ids")
    assert Ledger(usage).units("google.text_search.ids") == 1


# --- 10: honest provenance of the owner's records -------------------------------------------------------


class FakeGit:
    def __init__(self, tracked=("approvals.json",), changed=()):
        self.tracked, self.changed, self.calls = set(tracked), set(changed), []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        name = cmd[-1]
        if "ls-files" in cmd:
            return subprocess.CompletedProcess(cmd, 0 if name in self.tracked else 1, "", "")
        if "diff" in cmd:
            return subprocess.CompletedProcess(cmd, 1 if name in self.changed else 0, "", "")
        raise AssertionError(cmd)


def test_provenance_reports_tracked_and_changed_files(policy_dir, monkeypatch):
    for n in ("approvals.json", "stops.json", "usage.json", "KILL.json"):
        (policy_dir / n).write_text("[]")
    fake = FakeGit(tracked=("approvals.json", "usage.json", "KILL.json"), changed=("usage.json",))
    monkeypatch.setattr(policy.subprocess, "run", fake)
    prov = policy.records_provenance()
    files = prov["files"]
    assert set(files) == {"approvals.json", "stops.json", "usage.json", "KILL.json"}
    assert files["approvals.json"]["tracked"] == "yes" and files["approvals.json"]["same_as_commit"] == "yes"
    assert files["usage.json"]["tracked"] == "yes" and files["usage.json"]["same_as_commit"] == "no"
    assert files["stops.json"]["tracked"] == "no"
    assert prov["note"] == (
        "Records are files an agent with a shell can write. A record that is new or changed since the "
        "last commit was made in this session."
    )
    assert all(c[0] == "git" for c in fake.calls)
    assert not any(w in c for c in fake.calls for w in ("add", "commit", "checkout", "reset", "stash"))


@pytest.mark.parametrize("failure", [FileNotFoundError("git"), "not a repo"])
def test_provenance_is_unknown_without_git_or_outside_a_checkout(policy_dir, monkeypatch, failure):
    (policy_dir / "approvals.json").write_text("[]")

    def run(cmd, **kw):
        if isinstance(failure, Exception):
            raise failure
        return subprocess.CompletedProcess(cmd, 128, "", "fatal: not a git repository")

    monkeypatch.setattr(policy.subprocess, "run", run)
    files = policy.records_provenance()["files"]
    assert files["approvals.json"]["tracked"] == "unknown"
    assert files["approvals.json"]["same_as_commit"] == "unknown"


def test_status_and_doctor_show_the_provenance(policy_dir, monkeypatch):
    monkeypatch.setattr(policy.subprocess, "run", FakeGit())
    (policy_dir / "approvals.json").write_text("[]")
    r = invoke("policy", "status")
    assert r.exit_code == 0
    assert "records_provenance" in json.loads(r.stdout)
    r = invoke("doctor")
    assert r.exit_code == 0
    assert "records_provenance" in json.loads(r.stdout)["account_protection"]


def test_nothing_depends_on_the_provenance(spending_none, monkeypatch):
    def boom(*a, **k):
        raise OSError("no git")

    monkeypatch.setattr(policy.subprocess, "run", boom)
    Budget(Ledger(":memory:")).charge("google.aggregate")


# --- 11: tests that could not fail, made behavioural -----------------------------------------------------


def _always_failing_client():
    def boom(req):
        raise httpx.ConnectError("down")

    return httpx.Client(transport=httpx.MockTransport(boom))


@pytest.mark.parametrize(
    "make",
    [
        lambda b: GooglePlaces(
            b, api_key="AIzaK", client=_always_failing_client(), sleep=lambda s: None
        ).details("ChIJabcdefghij", tier="ids"),
        lambda b: PlacesAggregate(b, api_key="AIzaK", client=_always_failing_client()).count(
            BOX, types=["bakery"]
        ),
        lambda b: ApifyProvider(b, token="t", client=_always_failing_client()).text_search(
            Query("x"), bbox=BOX, max_results=5
        ),
    ],
)
def test_a_reservation_is_kept_when_the_send_raises(grant_spend, usage, make):
    grant_spend("google", "none")
    grant_spend("apify", "none")
    b = file_budget()
    with pytest.raises(ProviderError):
        make(b)
    assert b.requests == 1
    assert sum(Ledger(usage).units(s) for s in SKUS) >= 1  # the unit stays counted


def test_a_mutant_that_hands_the_reservation_back_is_caught(grant_spend, usage, monkeypatch):
    """The same check against a deliberately broken provider: it must fail the assertion."""
    grant_spend("google", "none")
    b = file_budget()
    p = GooglePlaces(b, api_key="AIzaK", client=_always_failing_client(), sleep=lambda s: None)
    original = p._client.request

    def mutant(*a, **k):
        try:
            return original(*a, **k)
        except httpx.HTTPError:
            with b.ledger.transaction() as data:  # the mutation: give the unit back
                row = data["months"][current_month()]["google.details.ids"]
                row["units"] -= 1
            raise

    monkeypatch.setattr(p._client, "request", mutant)
    with pytest.raises(ProviderError):
        p.details("ChIJabcdefghij", tier="ids")
    assert Ledger(usage).units("google.details.ids") == 0  # what the mutant leaves behind ...
    assert Ledger(usage).units("google.details.ids") != 1  # ... is what test_a_reservation_is_kept rejects


def test_a_real_refusal_leaves_every_counter_unchanged(spending_enabled, usage):
    Ledger(usage).add(ENT, 900, 0)  # the 90% reserve is used up
    b = file_budget()
    before = usage.read_bytes()
    with pytest.raises(BudgetExceeded, match="90%"):
        b.charge(ENT, fingerprint="x")
    assert usage.read_bytes() == before  # not a unit, not a day count, not a lease
    assert b.requests == 0 and b.run_units == {} and b.run_usd == 0
    mem = Ledger(":memory:")
    mem.add(ENT, 900, 0)
    snap = json.dumps(mem.read(), sort_keys=True, default=str)
    with pytest.raises(BudgetExceeded):
        Budget(mem).charge(ENT)
    assert json.dumps(mem.read(), sort_keys=True, default=str) == snap


def test_a_multi_sku_charge_is_all_or_nothing(grant_spend):
    grant_spend("apify", "enabled", per_search_cap_usd=50.0, period_cap_usd=0.005)
    mem = Ledger(":memory:")
    mem.add("apify.place", 1250, 0)
    b = Budget(mem, mode="quality", max_usd=50.0)  # the place fits under the period cap, the reviews do not
    snap = json.dumps(mem.read(), sort_keys=True, default=str)
    with pytest.raises(BudgetExceeded):
        b.charge_many([("apify.place", 1), ("apify.review", 5)])
    assert json.dumps(mem.read(), sort_keys=True, default=str) == snap


def test_the_mcp_server_exposes_only_allow_listed_read_and_search_commands(monkeypatch):
    pytest.importorskip("mcp")
    import asyncio

    from mcp import Client

    from maps import mcp_server

    allowed = [
        ("doctor",), ("policy", "status"), ("budget",), ("area",), ("search",), ("discover",), ("rank",),
        ("followups",), ("census",), ("enumerate",), ("details",), ("reviews", "fetch"), ("reviews", "search"),
        ("reviews", "integrity"), ("reviews", "contributors"), ("runs",), ("purge",),
    ]  # fmt: skip
    forbidden_words = ("approve", "resume", "reconcile", "restore", "clear-stop", "revoke", "kill")
    free_form = {"argv", "args", "command", "cmd", "arguments", "extra_args", "extra", "options", "flags"}
    seen: list[list[str]] = []
    monkeypatch.setattr(mcp_server, "run_cli", lambda argv: seen.append(list(argv)) or {})

    def sample(schema):
        if "enum" in schema:
            return schema["enum"][0]
        for key in ("anyOf", "oneOf"):
            if key in schema:
                return sample(next(s for s in schema[key] if s.get("type") != "null"))
        kind = schema.get("type")
        return {
            "string": "x", "integer": 1, "number": 1.0, "boolean": False, "array": ["x"], "object": {}
        }.get(kind, "x")  # fmt: skip

    async def go():
        names = []
        async with Client(mcp_server.server) as client:
            for tool in (await client.list_tools()).tools:
                schema = tool.input_schema
                props = schema.get("properties", {})
                assert not (set(props) & free_form), f"{tool.name} accepts a free-form parameter"
                assert schema.get("additionalProperties") in (None, False), tool.name
                args = {k: sample(props[k]) for k in schema.get("required", [])}
                seen.clear()
                await client.call_tool(tool.name, args)
                assert seen, f"{tool.name} did not run a command"
                argv = seen[0]
                assert any(argv[: len(p)] == list(p) for p in allowed), (tool.name, argv)
                assert not any(w in a for a in argv[:2] for w in forbidden_words), (tool.name, argv)
                if tool.name == "maps_budget":
                    assert argv == ["budget"]
                names.append(tool.name)
        return names

    names = asyncio.run(go())
    assert {"maps_budget", "maps_search", "maps_policy_status"} <= set(names)
    assert not any(
        w in n for n in names for w in ("approve", "resume", "reconcile", "restore", "kill", "clear")
    )
