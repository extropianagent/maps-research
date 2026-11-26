"""The spending gateway through the CLI and the MCP server: exit codes, the kill switch, reconciliation."""

from __future__ import annotations

import json
from datetime import date

import httpx
import pytest
from click.testing import CliRunner

from maps import cli, config, policy
from maps.budget import Budget, Ledger, current_month

BOX = "24.5,46.5,24.9,46.9"


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("MAPS_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("MAPS_RETENTION_DAYS", raising=False)
    for name in (
        "GOOGLE_MAPS_API_KEY",
        "APIFY_TOKEN",
        "DATAFORSEO_LOGIN",
        "DATAFORSEO_PASSWORD",
        "GEMINI_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def invoke(*args):
    return CliRunner().invoke(cli.main, [str(a) for a in args])


def run_cli(*args, expect=0):
    result = invoke(*args)
    assert result.exit_code == expect, f"{args}: exit {result.exit_code}\n{result.output}"
    return result


def jrun(*args):
    return json.loads(run_cli(*args).stdout)


@pytest.fixture
def google_wire(monkeypatch, grant_service_use, ai_host):
    """A Google search that can run: key, terms checked, AI host approved, and a counting mock transport."""
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "g-secret")
    grant_service_use("google")
    sent: list[httpx.Request] = []
    behaviour = {"handler": lambda req: httpx.Response(200, json={"places": []})}

    def handler(req):
        sent.append(req)
        return behaviour["handler"](req)

    from maps.providers import google_places

    monkeypatch.setattr(
        google_places, "make_client", lambda timeout: httpx.Client(transport=httpx.MockTransport(handler))
    )
    return sent, behaviour


def page(n, start=0, token=None):
    body = {"places": [{"id": f"ChIJ{start + i:012d}", "displayName": {"text": f"P{i}"}} for i in range(n)]}
    if token:
        body["nextPageToken"] = token
    return httpx.Response(200, json=body)


SEARCH = ("search", "-q", "bakery", "--bbox", BOX, "--depth", "standard", "--max-tasks", "1", "--tier", "ids")


# --- A9: exit codes -------------------------------------------------------------------------------


def test_max_usd_above_the_owners_cap_exits_2_with_spending(google_wire, grant_spend):
    sent, _ = google_wire
    grant_spend("google", "enabled", per_search_cap_usd=1.0)
    r = invoke(*SEARCH, "--cost", "quality", "--max-usd", "5")
    assert r.exit_code == 2
    assert r.stderr.strip().startswith("Spending:") and "per-search cap of $1.00" in r.stderr
    assert sent == []
    run_cli(*SEARCH, "--cost", "quality", "--max-usd", "1")  # exactly the cap is fine


def test_max_usd_is_only_checked_against_the_providers_in_use(grant_spend):
    grant_spend("google", "enabled", per_search_cap_usd=1.0)
    run_cli(
        "search",
        "-q",
        "bakery",
        "--provider",
        "sim",
        "--depth",
        "quick",
        "--cost",
        "quality",
        "--max-usd",
        "50",
    )


def test_no_spend_record_exits_2_before_any_request(google_wire):
    sent, _ = google_wire
    r = invoke(*SEARCH)
    assert r.exit_code == 2
    assert r.stderr.strip().startswith("Spending:")
    assert "no spending authorization recorded for google" in r.stderr
    assert "maps policy approve google --kind spend" in r.stderr
    assert sent == []
    assert not (config.policy_dir() / "usage.json").exists()  # a refusal creates nothing


def test_a_refusal_mid_run_exits_0_with_the_reason_in_coverage(google_wire, grant_spend):
    sent, behaviour = google_wire
    grant_spend("google", "enabled")
    behaviour["handler"] = lambda req: page(20, start=len(sent) * 20, token=f"t{len(sent)}")
    # the 90% reserve applies to the enterprise tier (the default)
    Ledger(config.ledger_path()).add("google.text_search.enterprise", 899, 0.0)
    doc = jrun("search", "-q", "bakery", "--bbox", BOX, "--depth", "standard", "--max-tasks", "1")
    assert doc["coverage"]["complete"] is False and "90%" in doc["coverage"]["stopped"]
    assert len(sent) == 1


def test_an_unusable_usage_file_exits_2_before_any_request(google_wire, grant_spend):
    sent, _ = google_wire
    grant_spend("google", "enabled")
    (config.policy_dir() / "usage.json").write_text("garbage")
    r = invoke(*SEARCH)
    assert r.exit_code == 2 and "usage.json" in r.stderr and r.stderr.strip().startswith("Spending:")
    assert sent == []


def test_a_kill_during_a_run_stops_it_at_the_next_request(google_wire, grant_spend):
    sent, behaviour = google_wire
    grant_spend("google", "none")

    def handler(req):
        if len(sent) == 1:
            policy.kill("boom")  # someone pulls the switch while the first page is in flight
        return page(20, start=len(sent) * 20, token=f"t{len(sent)}")

    behaviour["handler"] = handler
    doc = jrun(*SEARCH)
    assert "kill switch is on: boom" in doc["coverage"]["stopped"]
    assert len(sent) == 1


# --- A7: kill switch ------------------------------------------------------------------------------


def test_kill_and_resume_through_the_cli(policy_dir, google_wire, grant_spend):
    sent, _ = google_wire
    grant_spend("google", "none")
    out = jrun("policy", "kill", "--reason", "surprise charge")
    assert out["reason"] == "surprise charge" and (policy_dir / "KILL.json").exists()
    assert jrun("policy", "status")["kill_switch"]["on"] is True
    assert jrun("doctor")["account_protection"]["kill_switch"]["reason"] == "surprise charge"
    assert jrun("budget")["kill_switch"]["on"] is True
    r = invoke(*SEARCH)
    assert (
        r.exit_code == 2
        and "Account protection:" in r.stderr
        and "kill switch is on: surprise charge" in r.stderr
    )
    assert sent == []
    run_cli("search", "-q", "bakery", "--provider", "sim", "--depth", "quick")  # sim is exempt
    run_cli("policy", "resume", expect=2)  # needs the owner's statement
    assert (policy_dir / "KILL.json").exists()
    assert jrun("policy", "resume", "--owner-statement", "owner: all clear")["resumed"] is True
    assert not (policy_dir / "KILL.json").exists()
    assert jrun("policy", "status")["kill_switch"]["on"] is False
    run_cli(*SEARCH)
    assert len(sent) == 1


def test_kill_needs_no_owner_statement_but_a_reason(policy_dir):
    run_cli("policy", "kill", expect=2)
    run_cli("policy", "kill", "--reason", "  ", expect=2)
    assert not (policy_dir / "KILL.json").exists()


# --- A8: reconciliation and the budget report -----------------------------------------------------


def test_reconcile_raises_never_lowers(policy_dir):
    sku = "google.text_search.enterprise"
    doc = jrun("budget", "reconcile", "--sku", sku, "--units", "120", "--owner-statement", "console says 120")
    assert doc["units"] == 120 and doc["previous_units"] == 0 and doc["month"] == current_month()
    again = jrun("budget", "reconcile", "--sku", sku, "--units", "80", "--owner-statement", "console says 80")
    assert again["units"] == 120
    data = json.loads((policy_dir / "usage.json").read_text())
    assert data["months"][current_month()][sku]["units"] == 120
    assert [(r["sku"], r["units"], r["statement"]) for r in data["reconciliations"]] == [
        (sku, 120, "console says 120"),
        (sku, 80, "console says 80"),
    ]
    assert all(r["date"] == date.today().isoformat() for r in data["reconciliations"])


def test_reconcile_validation(policy_dir):
    base = ("budget", "reconcile", "--units", "5", "--owner-statement", "s")
    run_cli(*base, "--sku", "nope.nothing", expect=2)
    run_cli(*base, "--sku", "sim.search", expect=2)
    run_cli("budget", "reconcile", "--sku", "google.aggregate", "--units", "5", expect=2)  # no statement
    run_cli(
        "budget",
        "reconcile",
        "--sku",
        "google.aggregate",
        "--units",
        "-3",
        "--owner-statement",
        "s",
        expect=2,
    )
    run_cli(
        "budget",
        "reconcile",
        "--sku",
        "google.aggregate",
        "--units",
        "nan",
        "--owner-statement",
        "s",
        expect=2,
    )
    assert not (policy_dir / "usage.json").exists()


def test_reconciled_units_count_against_the_free_allowance(grant_spend, policy_dir):
    grant_spend("google", "enabled")
    run_cli("budget", "reconcile", "--sku", "google.aggregate", "--units", "4500", "--owner-statement", "s")
    b = Budget(Ledger(config.ledger_path()))
    from maps.budget import BudgetExceeded

    with pytest.raises(BudgetExceeded, match="90%"):
        b.charge("google.aggregate")


def test_budget_report_shows_records_caps_ceilings_and_kill_state(grant_spend):
    grant_spend("google", "enabled", per_search_cap_usd=2.0, period_cap_usd=20.0)
    grant_spend("apify", "none")
    run_cli(
        "budget",
        "reconcile",
        "--sku",
        "google.text_search.enterprise",
        "--units",
        "1500",
        "--owner-statement",
        "s",
    )
    doc = jrun("budget")
    g = doc["spend"]["google"]
    assert g["billing"] == "enabled" and g["per_search_cap_usd"] == 2.0 and g["period_cap_usd"] == 20.0
    assert g["month_out_of_pocket_usd"] == pytest.approx(500 * 0.035)
    assert doc["spend"]["apify"]["billing"] == "none"
    assert doc["spend"]["dataforseo"]["recorded"] is False
    assert doc["ceilings"]["max_requests"] == {"default": 400, "hard_maximum": 5000}
    assert doc["ceilings"]["by_depth"]["quick"]["max_requests"] == 30
    assert doc["kill_switch"]["on"] is False
    assert doc["rows"] and doc["month"] == current_month()


def test_depth_sets_the_per_run_ceilings():
    cost = cli.Cost("free", None)
    assert cli.make_budget(cost).max_requests == 400
    assert cli.make_budget(cost, depth="quick").max_requests == 30
    assert cli.make_budget(cost, depth="standard").max_requests == 200
    assert cli.make_budget(cost, depth="deep").max_requests == 1000
    ex = cli.make_budget(cost, depth="exhaustive")
    assert (ex.max_requests, ex.max_elapsed_s) == (5000, 3600)
    assert cli.make_budget(cost, depth="deep").max_elapsed_s == 900


# --- the approve command --------------------------------------------------------------------------


def test_approve_spend_through_the_cli(policy_dir):
    rec = jrun(
        "policy", "approve", "google", "--kind", "spend", "--owner-statement", "I accept up to two dollars",
        "--billing", "enabled", "--per-search-cap-usd", "2", "--period-cap-usd", "10",
        "--prices-checked-on", date.today().isoformat(), "--prices-source", "cloud.google.com/maps-platform/pricing",
    )  # fmt: skip
    assert rec["details"]["per_search_cap_usd"] == 2 and rec["details"]["billing"] == "enabled"
    assert policy.spend_record("google")
    jrun("policy", "approve", "apify", "--kind", "spend", "--owner-statement", "s", "--billing", "none")
    assert policy.spend_record("apify")["details"] == {"billing": "none"}
    run_cli(
        "policy",
        "approve",
        "google",
        "--kind",
        "spend",
        "--owner-statement",
        "s",
        "--billing",
        "enabled",
        expect=2,
    )
    run_cli("policy", "approve", "google", "--kind", "spend", "--owner-statement", "s", expect=2)
    run_cli("policy", "approve", "google", "--kind", "spend", "--billing", "none", expect=2)  # no statement


def test_approve_service_use_and_feature_through_the_cli():
    jrun(
        "policy",
        "approve",
        "google",
        "--kind",
        "service_use",
        "--owner-statement",
        "s",
        "--terms-checked",
        "read",
    )
    assert policy.approved("google", "service_use")
    jrun(
        "policy",
        "approve",
        "reviewer_matching",
        "--kind",
        "feature",
        "--owner-statement",
        "s",
        "--rights",
        "mine",
    )
    assert policy.approved("reviewer_matching", "feature")
    run_cli("policy", "approve", "google", "--kind", "service_use", "--owner-statement", "s", expect=2)
    run_cli("policy", "approve", "reviewer_matching", "--kind", "feature", "--owner-statement", "s", expect=2)


def test_approve_help_lists_every_kind_with_its_required_options():
    text = run_cli("policy", "approve", "--help").stdout
    for kind in ("provider", "ai_host", "egress_proxy", "service_use", "feature", "spend"):
        assert kind in text
    for opt in (
        "--data-rights", "--retrieval-method", "--downstream-use", "--data-handling", "--purpose",
        "--terms-checked", "--rights", "--billing", "--per-search-cap-usd", "--period-cap-usd",
        "--prices-checked-on", "--prices-source",
    ):  # fmt: skip
        assert opt in text


# --- MCP ------------------------------------------------------------------------------------------


def test_mcp_has_no_tool_to_approve_spending_resume_or_reconcile():
    pytest.importorskip("mcp")
    import asyncio

    from mcp import Client

    from maps import mcp_server

    async def go():
        async with Client(mcp_server.server) as client:
            return {t.name for t in (await client.list_tools()).tools}

    names = asyncio.run(go())
    assert "maps_budget" in names
    assert not any(
        w in n for n in names for w in ("approve", "resume", "reconcile", "kill", "clear_stop", "revoke")
    )


def test_mcp_search_above_the_cap_is_a_spending_error(google_wire, grant_spend):
    pytest.importorskip("mcp")
    from mcp.server.mcpserver.exceptions import ToolError

    from maps import mcp_server

    grant_spend("google", "enabled", per_search_cap_usd=1.0)
    with pytest.raises(ToolError, match="^Spending:"):
        mcp_server.run_cli(["search", "-q", "bakery", "--bbox", BOX, "--cost", "quality", "--max-usd", "9"])
