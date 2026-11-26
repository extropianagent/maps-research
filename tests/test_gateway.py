"""The spending gateway: one durable counter store, atomic reservations, recorded spending
authorization, unknown price means block, hard per-run ceilings, retries charged, kill switch.

Everything here is mock or synthetic: no network, and the repo's own `policy/` folder is never touched
(the autouse `policy_dir` fixture redirects it).
"""

from __future__ import annotations

import json
import threading
from datetime import date, timedelta

import httpx
import pytest

from maps import config, policy
from maps.budget import (
    DEPTH_CEILINGS,
    Budget,
    BudgetExceeded,
    GatewayRefusal,
    Ledger,
    current_month,
)
from maps.geography import BBox
from maps.models import Query
from maps.pricing import SKUS
from maps.providers.google_places import GooglePlaces

ENT = "google.text_search.enterprise"
BOX = BBox(24.0, 46.0, 25.0, 47.0)


@pytest.fixture
def usage(policy_dir):
    return policy_dir / "usage.json"


def file_budget(path=None, **kw):
    return Budget(Ledger(path or config.ledger_path()), **kw)


# --- A1: one durable counter store --------------------------------------------------------------


def test_the_ledger_lives_in_the_policy_folder(policy_dir):
    assert config.ledger_path() == policy_dir / "usage.json"


def test_the_store_holds_counters_only(spending_enabled, usage):
    file_budget().charge("google.text_search.pro", 2, fingerprint="a")
    doc = json.loads(usage.read_text())
    assert set(doc) == {"months", "reconciliations"}
    assert doc["months"][current_month()] == {"google.text_search.pro": {"units": 2, "usd": 0.0}}
    assert doc["reconciliations"] == []
    assert not list(usage.parent.glob("*.tmp"))  # atomic replace leaves no temp file behind
    assert (usage.parent / "usage.json.lock").exists()


def test_a_new_budget_on_the_same_file_continues_from_the_recorded_totals(spending_enabled, usage):
    Ledger(usage).add(ENT, 899, 0.0)  # 899 of the 900 allowed by the 90% reserve
    file_budget().charge(ENT)  # the 900th
    assert Ledger(usage).units(ENT) == 900
    with pytest.raises(BudgetExceeded, match="90%"):
        file_budget().charge(ENT)  # a restart does not start from zero
    assert Ledger(usage).units(ENT) == 900


def test_an_unparsable_store_blocks_every_chargeable_request(spending_enabled, usage):
    usage.write_text("{not json")
    with pytest.raises(BudgetExceeded, match="usage.json"):
        file_budget().charge("google.text_search.pro")
    assert usage.read_text() == "{not json"  # never overwritten


@pytest.mark.parametrize(
    "content",
    [
        "[]",
        '{"months": []}',
        '{"months": {"2026-11": {"google.aggregate": {"units": "x", "usd": 0}}}}',
        '{"months": {}}',
    ],
)
def test_a_malformed_store_blocks(spending_enabled, usage, content):
    usage.write_text(content)
    if content == '{"months": {}}':  # missing "reconciliations" is tolerated as empty
        file_budget().charge("google.aggregate")
        return
    with pytest.raises(BudgetExceeded, match="usage.json"):
        file_budget().charge("google.aggregate")


def test_a_store_that_cannot_be_used_blocks(spending_enabled, usage):
    usage.mkdir()  # a directory where the file should be
    with pytest.raises(BudgetExceeded, match="usage.json"):
        file_budget().charge("google.aggregate")


def test_sim_never_touches_the_store(usage):
    b = file_budget()
    b.charge("sim.search")
    b.charge("sim.reviews", 3)
    assert not usage.exists() and not usage.parent.joinpath("usage.json.lock").exists()
    assert b.run_units == {"sim.search": 1, "sim.reviews": 3}


def test_memory_ledger_still_works_for_unit_tests(spending_enabled):
    ledger = Ledger(":memory:")
    Budget(ledger).charge("google.aggregate", 3, fingerprint="x")
    assert ledger.units("google.aggregate") == 3


# --- A2: atomic reservation ---------------------------------------------------------------------


def test_two_budgets_racing_for_the_last_unit_do_not_both_succeed(spending_enabled, usage):
    Ledger(usage).add(ENT, 899, 0.0)
    results: list[object] = []
    barrier = threading.Barrier(2)

    def go():
        b = file_budget()  # its own Ledger object on the same file
        barrier.wait()
        try:
            results.append(b.charge(ENT, fingerprint=None))
        except BudgetExceeded as e:
            results.append(e)

    threads = [threading.Thread(target=go) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sum(isinstance(r, BudgetExceeded) for r in results) == 1
    assert Ledger(usage).units(ENT) == 900


def test_many_budgets_racing_get_exactly_the_free_units(spending_enabled, usage):
    Ledger(usage).add(ENT, 895, 0.0)  # five free units left under the reserve
    outcomes: list[bool] = []
    barrier = threading.Barrier(12)

    def go():
        b = file_budget()
        barrier.wait()
        try:
            b.charge(ENT)
            outcomes.append(True)
        except BudgetExceeded:
            outcomes.append(False)

    threads = [threading.Thread(target=go) for _ in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert outcomes.count(True) == 5
    assert Ledger(usage).units(ENT) == 900


def test_a_second_budget_sees_the_first_ones_reservation(spending_enabled, usage):
    Ledger(usage).add(ENT, 899, 0.0)
    a, b = file_budget(), file_budget()
    a.charge(ENT)
    with pytest.raises(BudgetExceeded):
        b.charge(ENT)


def test_a_reservation_is_never_released(spending_enabled, usage):
    b = file_budget()
    b.charge("google.aggregate")
    assert not any(hasattr(b, n) for n in ("release", "refund", "rollback", "undo"))
    assert Ledger(usage).units("google.aggregate") == 1


# --- A3: spending authorization is recorded by the owner ------------------------------------------

SUBJECT_CASES = [
    ("google", "google.text_search.pro"),
    ("google", "google.aggregate"),
    ("google", "google.agent_tools.search"),
    ("gemini", "google.gemini_maps_grounding"),
    ("apify", "apify.place"),
    ("apify", "apify.contributor"),
    ("dataforseo", "dataforseo.maps_search"),
]


def test_spend_subjects():
    for name in ("google", "google_aggregate", "google_agent_tools", "google.text_search.pro"):
        assert policy.spend_subject(name) == "google"
    for name in ("gemini_maps", "google.gemini_maps_grounding"):
        assert policy.spend_subject(name) == "gemini"
    for name in ("apify", "apify_contributor", "apify.place", "apify.contributor"):
        assert policy.spend_subject(name) == "apify"
    assert policy.spend_subject("dataforseo") == policy.spend_subject("dataforseo.reviews") == "dataforseo"
    assert policy.spend_subject("sim") is None and policy.spend_subject("sim.search") is None


@pytest.mark.parametrize(("subject", "sku"), SUBJECT_CASES)
def test_no_spend_record_means_no_chargeable_request(subject, sku):
    with pytest.raises(BudgetExceeded) as e:
        Budget(Ledger(":memory:")).charge(sku)
    msg = str(e.value)
    assert f"no spending authorization recorded for {subject}" in msg
    assert f"maps policy approve {subject} --kind spend" in msg


def test_the_check_method_refuses_the_same_way():
    with pytest.raises(BudgetExceeded, match="no spending authorization"):
        Budget(Ledger(":memory:")).check("google.aggregate")


def test_sim_needs_no_record_at_all():
    Budget(Ledger(":memory:")).charge("sim.search")


def test_billing_none_allows_a_price_unknown_sku_and_never_counts_as_out_of_pocket(grant_spend):
    grant_spend("google", "none")
    b = Budget(Ledger(":memory:"), mode="quality", max_usd=0)
    q = b.charge("google.agent_tools.search", fingerprint="a")
    assert q.out_of_pocket_usd == 0 and b.run_usd == 0
    assert b.ledger.units("google.agent_tools.search") == 1  # units are still counted


def test_billing_none_allows_a_sku_with_no_free_allowance_in_free_mode(grant_spend):
    grant_spend("dataforseo", "none")
    b = Budget(Ledger(":memory:"))
    for i in range(50):
        b.charge("dataforseo.maps_search", fingerprint=str(i))
    assert b.run_usd == 0 and b.ledger.units("dataforseo.maps_search") == 50


def test_billing_none_still_obeys_every_ceiling(grant_spend):
    grant_spend("apify", "none")
    b = Budget(Ledger(":memory:"), max_requests=2)
    b.charge("apify.place", 5)
    b.charge("apify.place", 5)
    with pytest.raises(BudgetExceeded, match="max_requests"):
        b.charge("apify.place", 5)


def test_billing_enabled_prices_older_than_90_days_block(grant_spend):
    old = (date.today() - timedelta(days=91)).isoformat()
    grant_spend("google", prices_checked_on=old)
    with pytest.raises(
        BudgetExceeded, match="prices not confirmed within 90 days: unknown price means block"
    ):
        Budget(Ledger(":memory:")).charge("google.text_search.pro")


def test_prices_checked_exactly_90_days_ago_are_still_fine(grant_spend):
    grant_spend("google", prices_checked_on=(date.today() - timedelta(days=90)).isoformat())
    Budget(Ledger(":memory:")).charge("google.text_search.pro")


def test_prices_dated_in_the_future_block(grant_spend):
    grant_spend("google", prices_checked_on=(date.today() + timedelta(days=1)).isoformat())
    with pytest.raises(BudgetExceeded, match="90 days"):
        Budget(Ledger(":memory:")).charge("google.text_search.pro")


@pytest.mark.parametrize(
    ("subject", "sku"),
    [
        ("google", "google.agent_tools.search"),
        ("gemini", "google.gemini_maps_grounding"),
        ("apify", "apify.contributor"),
    ],
)
def test_a_price_unknown_sku_is_blocked_under_billing_enabled(grant_spend, subject, sku):
    grant_spend(subject, "enabled")
    with pytest.raises(BudgetExceeded, match="unknown price means block"):
        Budget(Ledger(":memory:")).charge(sku)


def test_price_known_flags():
    unknown = {n for n, s in SKUS.items() if not s.price_known}
    assert unknown == {"google.agent_tools.search", "google.gemini_maps_grounding", "apify.contributor"}


def test_free_mode_blocks_once_the_free_allowance_is_exhausted(grant_spend):
    grant_spend("google", "enabled")
    ledger = Ledger(":memory:")
    ledger.add(ENT, SKUS[ENT].free_units, 0.0)
    with pytest.raises(BudgetExceeded, match="free allowance exhausted"):
        Budget(ledger).charge(ENT)
    assert ledger.units(ENT) == SKUS[ENT].free_units


def test_quality_mode_cannot_exceed_the_owners_per_search_cap(grant_spend):
    grant_spend("google", per_search_cap_usd=2.0)
    with pytest.raises(GatewayRefusal, match="per-search cap of \\$2.00"):
        Budget(Ledger(":memory:"), mode="quality", max_usd=2.5)
    Budget(Ledger(":memory:"), mode="quality", max_usd=2.0)  # exactly the cap is fine
    with pytest.raises(GatewayRefusal):
        Budget(Ledger(":memory:"), mode="quality", max_usd=2.5, subjects=["google"])


def test_the_per_search_cap_is_checked_only_for_the_subjects_in_use(grant_spend):
    grant_spend("google", per_search_cap_usd=1.0)
    grant_spend("dataforseo", per_search_cap_usd=50.0)
    Budget(Ledger(":memory:"), mode="quality", max_usd=20, subjects=["dataforseo"])
    with pytest.raises(GatewayRefusal):
        Budget(Ledger(":memory:"), mode="quality", max_usd=20, subjects=["dataforseo", "google"])


def test_the_period_cap_stops_a_run_part_way(grant_spend):
    grant_spend("dataforseo", per_search_cap_usd=5.0, period_cap_usd=0.01)
    b = Budget(Ledger(":memory:"), mode="quality", max_usd=5.0)
    for i in range(5):
        b.charge("dataforseo.maps_search", fingerprint=str(i))  # 5 x $0.002 = the whole $0.01
    with pytest.raises(BudgetExceeded, match="period cap"):
        b.charge("dataforseo.maps_search", fingerprint="6")
    assert b.ledger.units("dataforseo.maps_search") == 5


def test_the_period_cap_survives_a_restart(grant_spend, usage):
    grant_spend("dataforseo", per_search_cap_usd=5.0, period_cap_usd=0.01)
    first = file_budget(mode="quality", max_usd=5.0)
    for i in range(5):
        first.charge("dataforseo.maps_search", fingerprint=str(i))
    with pytest.raises(BudgetExceeded, match="period cap"):
        file_budget(mode="quality", max_usd=5.0).charge("dataforseo.maps_search")


def test_the_period_cap_counts_the_billing_month_only(grant_spend, usage, monkeypatch):
    from maps import budget as budget_mod

    grant_spend("dataforseo", per_search_cap_usd=5.0, period_cap_usd=0.002)
    monkeypatch.setattr(budget_mod, "current_month", lambda: "2026-10")
    file_budget(mode="quality", max_usd=5).charge("dataforseo.maps_search")
    monkeypatch.setattr(budget_mod, "current_month", lambda: "2026-11")
    file_budget(mode="quality", max_usd=5).charge("dataforseo.maps_search")  # a new month, a new period


def test_an_incomplete_spend_record_blocks(policy_dir):
    (policy_dir / "approvals.json").write_text(
        json.dumps(
            [
                {
                    "subject": "google",
                    "kind": "spend",
                    "owner_statement": "ok",
                    "details": {"billing": "enabled"},
                }
            ]
        )
    )
    with pytest.raises(BudgetExceeded, match="incomplete"):
        Budget(Ledger(":memory:")).charge("google.aggregate")


def test_approve_spend_validation():
    ok = {
        "billing": "enabled",
        "per_search_cap_usd": 1,
        "period_cap_usd": 5,
        "prices_checked_on": "2026-11-01",
        "prices_source": "page",
    }
    with pytest.raises(policy.PolicyViolation, match="spending"):
        policy.approve("sim", "spend", owner_statement="x", details={"billing": "none"})
    with pytest.raises(policy.PolicyViolation, match="billing"):
        policy.approve("google", "spend", owner_statement="x", details={"billing": "maybe"})
    for missing in ("per_search_cap_usd", "period_cap_usd", "prices_checked_on", "prices_source"):
        d = {k: v for k, v in ok.items() if k != missing}
        with pytest.raises(policy.PolicyViolation, match=missing):
            policy.approve("google", "spend", owner_statement="x", details=d)
    for bad in (-1, float("nan"), float("inf"), "5", True):
        with pytest.raises(policy.PolicyViolation, match="per_search_cap_usd"):
            policy.approve("google", "spend", owner_statement="x", details={**ok, "per_search_cap_usd": bad})
    with pytest.raises(policy.PolicyViolation, match="prices_checked_on"):
        policy.approve(
            "google", "spend", owner_statement="x", details={**ok, "prices_checked_on": "yesterday"}
        )
    with pytest.raises(policy.PolicyViolation, match="prices_source"):
        policy.approve("google", "spend", owner_statement="x", details={**ok, "prices_source": "  "})
    rec = policy.approve("google", "spend", owner_statement="x", details=ok)
    assert rec["details"] == ok
    none = policy.approve(
        "apify", "spend", owner_statement="x", details={"billing": "none", "period_cap_usd": 9}
    )
    assert none["details"] == {"billing": "none"}
    assert policy.spend_record("apify")["details"] == {"billing": "none"}


# --- A5: hard per-run ceilings ------------------------------------------------------------------


def test_default_ceilings_and_the_hard_maximum(grant_spend):
    grant_spend("google", "none")
    b = Budget(Ledger(":memory:"))
    assert (b.max_requests, b.max_elapsed_s) == (400, 900)
    assert Budget(Ledger(":memory:"), max_requests=5000).max_requests == 5000
    with pytest.raises(ValueError, match="5,000"):
        Budget(Ledger(":memory:"), max_requests=5001)
    with pytest.raises(ValueError):
        Budget(Ledger(":memory:"), max_requests=0)


def test_depth_ceilings():
    assert {k: v[0] for k, v in DEPTH_CEILINGS.items()} == {
        "quick": 30,
        "standard": 200,
        "deep": 1000,
        "exhaustive": 5000,
    }
    assert DEPTH_CEILINGS["exhaustive"][1] == 3600
    assert all(v[1] == 900 for k, v in DEPTH_CEILINGS.items() if k != "exhaustive")


def test_max_requests_stops_a_run(grant_spend):
    grant_spend("google", "none")
    b = Budget(Ledger(":memory:"), max_requests=400)
    for _ in range(400):
        b.charge("google.text_search.ids")
    with pytest.raises(BudgetExceeded, match="max_requests ceiling \\(400\\)"):
        b.charge("google.text_search.ids")
    assert b.requests == 400


def test_a_refused_charge_is_not_counted_as_a_request(grant_spend):
    grant_spend("dataforseo", "none")
    b = Budget(Ledger(":memory:"), max_requests=3)
    with pytest.raises(ValueError):
        b.charge("dataforseo.maps_search", float("nan"))
    assert b.requests == 0


def test_the_elapsed_time_ceiling_uses_the_injected_clock(grant_spend):
    grant_spend("google", "none")
    now = [1000.0]
    b = Budget(Ledger(":memory:"), clock=lambda: now[0])
    now[0] = 1000.0 + 899
    b.charge("google.aggregate", fingerprint="a")
    now[0] = 1000.0 + 901
    with pytest.raises(BudgetExceeded, match="max_elapsed_s ceiling \\(900"):
        b.charge("google.aggregate", fingerprint="b")
    assert b.ledger.units("google.aggregate") == 1


def test_the_same_fingerprint_more_than_three_times_is_refused(grant_spend):
    grant_spend("google", "none")
    b = Budget(Ledger(":memory:"))
    for _ in range(3):
        b.charge("google.aggregate", fingerprint="same")
    with pytest.raises(BudgetExceeded, match="repeated identical request"):
        b.charge("google.aggregate", fingerprint="same")
    b.charge("google.aggregate", fingerprint="other")
    b.charge("google.aggregate")  # no fingerprint: not tracked
    b.charge("google.aggregate")
    assert b.ledger.units("google.aggregate") == 6


def test_the_ceilings_do_not_apply_to_sim():
    b = Budget(Ledger(":memory:"), max_requests=1)
    for _ in range(10):
        b.charge("sim.search", fingerprint="same")
    assert b.requests == 0


def test_fingerprint_is_a_short_hash_without_secrets():
    fp = Budget.fingerprint("POST", "https://x.example/a", {"q": "bakery", "pageToken": "t1"})
    assert len(fp) <= 16 and fp == Budget.fingerprint(
        "POST", "https://x.example/a", {"pageToken": "t1", "q": "bakery"}
    )
    assert fp != Budget.fingerprint("POST", "https://x.example/a", {"q": "bakery", "pageToken": "t2"})
    assert fp != Budget.fingerprint("GET", "https://x.example/a", {"q": "bakery", "pageToken": "t1"})
    assert "bakery" not in fp and "x.example" not in fp


# --- ceilings through a real provider and the retrieval loop ------------------------------------


def google(handler, budget, **kw):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    sleeps: list[float] = []
    p = GooglePlaces(budget, api_key="K-secret", client=client, sleep=sleeps.append, **kw)
    p.sleeps = sleeps  # type: ignore[attr-defined]
    return p


def test_pagination_runaway_is_stopped_by_max_requests(grant_spend):
    grant_spend("google", "none")
    sent: list[httpx.Request] = []

    def handler(req):
        sent.append(req)
        return httpx.Response(200, json={"nextPageToken": f"t{len(sent)}"})  # never any places, never an end

    p = google(handler, Budget(Ledger(":memory:"), max_requests=10))
    with pytest.raises(BudgetExceeded, match="max_requests"):
        p.text_search(Query("bakery", "en"), bbox=BOX)
    assert len(sent) == 10


def test_a_page_token_loop_is_stopped_by_the_fingerprint_rule(grant_spend):
    grant_spend("google", "none")
    sent: list[httpx.Request] = []

    def handler(req):
        sent.append(req)
        return httpx.Response(200, json={"nextPageToken": "same"})

    p = google(handler, Budget(Ledger(":memory:")))
    with pytest.raises(BudgetExceeded, match="repeated identical request"):
        p.text_search(Query("bakery", "en"), bbox=BOX)
    assert len(sent) == 4  # the first page, then the identical second page three times


def test_a_retry_storm_is_stopped_by_the_fingerprint_rule(grant_spend):
    grant_spend("google", "none")
    sent: list[httpx.Request] = []

    def handler(req):
        sent.append(req)
        return httpx.Response(503, json={"error": {"message": "unavailable"}})

    b = Budget(Ledger(":memory:"))
    p = google(handler, b)
    for _ in range(2):  # each call is a request and one retry, all of them identical
        try:
            p.details("ChIJplace0001", tier="ids")
        except Exception:
            pass
    assert len(sent) == 3  # the fourth identical request, the second call's retry, never left
    assert b.ledger.units("google.details.ids") == 3


def test_ceiling_stops_a_discovery_run_and_is_reported_in_coverage(grant_spend):
    from maps import retrieval

    grant_spend("google", "none")
    n = [0]

    def handler2(req):
        n[0] += 1
        body = {"places": [{"id": f"ChIJ{n[0]:04d}{i:06d}", "displayName": {"text": "n"}} for i in range(20)]}
        body["nextPageToken"] = f"t{n[0]}"
        return httpx.Response(200, json=body)

    p = google(handler2, Budget(Ledger(":memory:"), max_requests=2))
    res = retrieval.discover([p], [Query("bakery", "en")], BOX, retrieval.DEPTHS["standard"])
    cov = retrieval.coverage_summary(res)
    assert cov["complete"] is False and "max_requests" in cov["stopped"]
    assert n[0] == 2


# --- A6: every retry is charged -------------------------------------------------------------------


def test_a_retry_is_charged_again(grant_spend):
    grant_spend("google", "none")
    replies = iter(
        [httpx.Response(503, json={"error": {"message": "x"}}), httpx.Response(200, json={"places": []})]
    )
    sent: list[httpx.Request] = []

    def handler(req):
        sent.append(req)
        return next(replies)

    b = Budget(Ledger(":memory:"))
    p = google(handler, b)
    p.details("ChIJplace0001", tier="ids")
    assert len(sent) == 2 and b.ledger.units("google.details.ids") == 2 and b.requests == 2
    assert p.sleeps == [2]


def test_a_retry_that_the_budget_refuses_is_not_sent(grant_spend):
    grant_spend("google", "none")
    sent: list[httpx.Request] = []

    def handler(req):
        sent.append(req)
        return httpx.Response(429, json={"error": {"message": "slow down"}})

    p = google(handler, Budget(Ledger(":memory:"), max_requests=1))
    with pytest.raises(BudgetExceeded, match="max_requests"):
        p.details("ChIJplace0001", tier="ids")
    assert len(sent) == 1 and p.sleeps == []


# --- A7: kill switch ------------------------------------------------------------------------------


def test_kill_and_resume(policy_dir, grant_spend):
    grant_spend("google", "none")
    assert policy.kill_status() == {"on": False, "reason": None, "since": None}
    policy.kill("owner saw a surprise charge")
    assert (policy_dir / "KILL.json").exists()
    st = policy.kill_status()
    assert st["on"] is True and st["reason"] == "owner saw a surprise charge" and st["since"]
    with pytest.raises(BudgetExceeded, match="kill switch is on: owner saw a surprise charge"):
        Budget(Ledger(":memory:")).charge("google.aggregate")
    with pytest.raises(policy.PolicyViolation, match="kill switch is on: owner saw"):
        policy.require_provider("google")
    policy.require_provider("sim")  # the simulator is exempt
    Budget(Ledger(":memory:")).charge("sim.search")
    with pytest.raises(policy.PolicyViolation):
        policy.resume(owner_statement="  ")
    assert policy.resume(owner_statement="owner: all clear") is True
    assert not (policy_dir / "KILL.json").exists()
    Budget(Ledger(":memory:")).charge("google.aggregate")
    assert policy.resume(owner_statement="owner: again") is False  # nothing to remove


def test_kill_needs_a_reason(policy_dir):
    with pytest.raises(policy.PolicyViolation, match="reason"):
        policy.kill("  ")
    assert not (policy_dir / "KILL.json").exists()


def test_an_unreadable_kill_file_counts_as_on(policy_dir, grant_spend):
    grant_spend("google", "none")
    (policy_dir / "KILL.json").write_text("{broken")
    assert policy.kill_status()["on"] is True
    with pytest.raises(BudgetExceeded, match="kill switch is on"):
        Budget(Ledger(":memory:")).charge("google.aggregate")


def _every_provider_with_a_counting_transport(budget):
    """One of each chargeable provider over a transport that counts what leaves the process."""
    from maps.providers.apify import ApifyProvider
    from maps.providers.dataforseo import DataForSEOProvider
    from maps.providers.gemini_maps import GeminiMaps
    from maps.providers.google_agent_tools import MapsAgentTools
    from maps.providers.google_aggregate import PlacesAggregate

    sent: list[httpx.Request] = []

    def handler(req):
        sent.append(req)
        return httpx.Response(200, json={})

    def client():
        return httpx.Client(transport=httpx.MockTransport(handler))

    q = Query("bakery", "en")
    calls = {
        "google": lambda: google(handler, budget).text_search(q, bbox=BOX),
        "google_aggregate": lambda: PlacesAggregate(budget, api_key="k", client=client()).count(
            BOX, types=["bakery"]
        ),
        "google_agent_tools": lambda: MapsAgentTools(budget, api_key="k", client=client()).search_places(
            "bakery"
        ),
        "gemini_maps": lambda: GeminiMaps(budget, api_key="k", client=client()).search_places("bakery"),
        "apify": lambda: ApifyProvider(budget, token="t", client=client()).text_search(
            q, bbox=BOX, max_results=3
        ),
        "dataforseo": lambda: DataForSEOProvider(
            budget, login="l", password="p", client=client(), sleep=lambda s: None
        ).text_search(q, bbox=BOX),
    }
    return calls, sent


def test_with_the_kill_switch_on_no_provider_sends_anything(spending_none):
    policy.kill("stop everything")
    calls, sent = _every_provider_with_a_counting_transport(Budget(Ledger(":memory:")))
    for call in calls.values():
        with pytest.raises(BudgetExceeded, match="kill switch is on"):
            call()
    assert sent == []


def test_after_resume_a_request_goes_out_again(spending_none):
    policy.kill("stop everything")
    calls, sent = _every_provider_with_a_counting_transport(Budget(Ledger(":memory:")))
    with pytest.raises(BudgetExceeded):
        calls["google"]()
    assert sent == []
    policy.resume(owner_statement="owner: back on")
    calls["google"]()
    assert len(sent) == 1


@pytest.mark.parametrize(
    "name", ["google", "google_aggregate", "google_agent_tools", "gemini_maps", "apify", "dataforseo"]
)
def test_with_no_spend_record_no_request_leaves_the_process(name):
    calls, sent = _every_provider_with_a_counting_transport(Budget(Ledger(":memory:")))
    with pytest.raises(BudgetExceeded, match="no spending authorization recorded"):
        calls[name]()
    assert sent == []  # not even the free setup calls of the agent-tools client
