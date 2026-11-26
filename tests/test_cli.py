import json
import os
import time

import pytest
from click.testing import CliRunner

from maps import cli
from maps.budget import BudgetExceeded, Ledger
from maps.config import ledger_path, state_dir
from maps.geography import BBox
from maps.models import Hit, Place, Review
from maps.providers.base import PolicyStop, ProviderError, QuotaExceeded, SearchPage
from maps.providers.google_agent_tools import AgentSearchResult
from maps.providers.google_aggregate import EnumerationResult
from maps.semantic import DEFAULT_MODEL, SemanticUnavailable
from maps.store import RunStore, list_runs

SECRETS = {
    "GOOGLE_MAPS_API_KEY": "g-secret-VALUE-1",
    "APIFY_TOKEN": "apify-secret-VALUE-2",
    "DATAFORSEO_LOGIN": "dfs-login-VALUE-3",
    "DATAFORSEO_PASSWORD": "dfs-pass-VALUE-4",
}


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("MAPS_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("MAPS_RETENTION_DAYS", raising=False)
    for name in SECRETS:
        monkeypatch.delenv(name, raising=False)
    return tmp_path


@pytest.fixture(autouse=True)
def recorded(policy_dir, monkeypatch, spending_none, grant_service_use):
    """The owner's records the real providers need: Google service use, spending (billing none: the
    gateway's own rules are tested in test_gateway*.py) and an approved AI host. Tests of default-deny
    remove what they test."""
    from maps import policy

    grant_service_use()
    monkeypatch.setenv("MAPS_AI_HOST", "testhost")
    policy.approve(
        "testhost", "ai_host", owner_statement="test owner approves", details={"data_handling": "no training"}
    )


def run_cli(*args, expect=0):
    result = CliRunner().invoke(cli.main, [str(a) for a in args])
    assert result.exit_code == expect, f"{args}: exit {result.exit_code}\n{result.output}"
    return result


def jrun(*args):
    return json.loads(run_cli(*args).stdout)


def sim_search(*extra, q=("bakery",)):
    qs = [x for query in q for x in ("-q", query)]
    return jrun("search", *qs, "--provider", "sim", *extra)


# --- doctor / budget / help -------------------------------------------------------------------


def test_help_documents_every_command_and_its_cost():
    out = run_cli("--help").stdout
    for cmd in (
        "doctor",
        "budget",
        "area",
        "search",
        "rank",
        "followups",
        "census",
        "enumerate",
        "details",
        "reviews",
        "runs",
        "purge",
        "eval",
    ):
        assert cmd in out
    for cmd in ("search", "rank", "census", "details"):
        assert "cost" in run_cli(cmd, "--help").stdout.lower()


def test_doctor_shape_and_no_secret_values(monkeypatch):
    for k, v in SECRETS.items():
        monkeypatch.setenv(k, v)
    result = run_cli("doctor")
    doc = json.loads(result.stdout)
    assert set(doc) >= {"version", "state_dir", "retention_days", "providers", "prices_checked", "runs"}
    assert set(doc["providers"]) == {
        "google",
        "google_aggregate",
        "google_agent_tools",
        "gemini_maps",
        "apify",
        "dataforseo",
        "sim",
    }

    # the approval and spend-record state of each provider is covered in test_default_deny.py
    def basics(name):
        return {k: doc["providers"][name][k] for k in ("available", "env", "class")}

    assert basics("google_agent_tools") == {
        "available": True,
        "env": ["GOOGLE_MAPS_API_KEY"],
        "class": "core",
    }
    assert basics("google") == {"available": True, "env": ["GOOGLE_MAPS_API_KEY"], "class": "core"}
    assert basics("gemini_maps") == {"available": False, "env": ["GEMINI_API_KEY"], "class": "core"}
    assert doc["providers"]["dataforseo"]["env"] == ["DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD"]
    assert doc["providers"]["sim"]["available"] is True
    for v in SECRETS.values():
        assert v not in result.output


def test_doctor_reports_unconfigured_providers():
    doc = jrun("doctor")
    assert doc["providers"]["google"]["available"] is False and doc["runs"] == 0


def test_budget_lists_used_and_free_rows_as_valid_json():
    sim_search("--depth", "quick")
    result = run_cli("budget")
    assert "Infinity" not in result.stdout
    doc = json.loads(result.stdout)
    skus = {r["sku"] for r in doc["rows"]}
    # the simulator is exempt from the gateway and never recorded in the usage store
    assert "sim.search" not in skus and "google.text_search.enterprise" in skus
    assert "google.text_search.ids" not in skus  # unlimited and unused: nothing to report
    assert "mode" in doc["note"].lower()


# --- search -----------------------------------------------------------------------------------


def test_sim_search_returns_documented_shape_and_stores_a_run(state):
    doc = sim_search("--depth", "quick", "--top", "5")
    assert set(doc) >= {"run_id", "coverage", "budget", "filters", "objective", "matched", "results", "next"}
    assert len(doc["results"]) == 5 and doc["matched"] >= 5
    assert doc["coverage"]["searches_run"] >= 1
    assert (state / "runs" / doc["run_id"] / "spec.json").exists()
    assert (state / "runs" / doc["run_id"] / "tasks.jsonl").exists()
    row = doc["results"][0]
    assert row["rank"] == 1 and "place_id" in row and "seen_by" in row and "parts" in row
    assert any(doc["run_id"] in n for n in doc["next"])
    assert any("followups" in n for n in doc["next"]) and any("reviews fetch" in n for n in doc["next"])


def test_incomplete_coverage_suggests_resuming_deeper():
    doc = sim_search("--depth", "standard", "--max-tasks", "2")
    assert not doc["coverage"]["complete"]
    assert any("--resume" in n and "--depth deep" in n for n in doc["next"])


def test_spec_records_what_was_asked(state):
    doc = sim_search("--depth", "quick", q=("bakery", "مخبز:ar"))
    spec = RunStore.open(state, doc["run_id"]).spec
    assert spec["queries"] == ["bakery", "مخبز:ar"] and spec["depth"] == "quick"
    assert spec["providers"] == ["sim"] and spec["cost"] == "free"
    assert BBox.parse(spec["bbox"]) is not None


def test_exact_filters_apply_to_every_result():
    doc = sim_search("--depth", "standard", "--max-rating", "4.5", "--min-reviews", "10", "--top", "50")
    assert doc["results"] and doc["filters"] == {"max_rating": 4.5, "min_reviews": 10}
    for r in doc["results"]:
        assert r["rating"] <= 4.5 and r["reviews"] >= 10


def test_type_and_name_filters():
    doc = sim_search("--depth", "quick", "--type", "bakery", "--name-regex", "^bakery", "--top", "50")
    assert doc["results"] and all(r["type"] == "bakery" for r in doc["results"])
    none = sim_search("--depth", "quick", "--exclude-type", "bakery")
    assert none["matched"] == 0


def test_objectives_order_differently():
    best = sim_search("--depth", "standard", "--objective", "best", "--top", "10")
    gems = sim_search("--depth", "standard", "--objective", "gems", "--top", "10")
    assert gems["objective"] == "gems"
    assert [r["place_id"] for r in best["results"]] != [r["place_id"] for r in gems["results"]]


def test_markdown_format_has_header_and_table():
    out = run_cli("search", "-q", "bakery", "--provider", "sim", "--depth", "quick", "--format", "md").stdout
    assert "| # | Name | Rating | Reviews | Score | Type | Hidden? |" in out
    assert "run" in out.lower() and "coverage" in out.lower()
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_progress_goes_to_stderr_not_stdout():
    result = run_cli("search", "-q", "bakery", "--provider", "sim", "--depth", "quick")
    assert "results" in result.stderr and "sim" in result.stderr
    json.loads(result.stdout)


def test_multilingual_queries_find_more_than_english_alone():
    en = sim_search("--depth", "deep", q=("bakery",))
    both = sim_search("--depth", "deep", q=("bakery", "مخبز:ar"))
    assert both["coverage"]["places"] > en["coverage"]["places"]


def test_max_tasks_then_resume_replays_and_completes():
    first = sim_search("--depth", "standard", "--max-tasks", "2")
    assert first["coverage"]["searches_run"] == 2 and first["coverage"]["stopped"]
    second = jrun("search", "--resume", first["run_id"])
    assert second["run_id"] == first["run_id"]
    assert second["coverage"]["searches_resumed"] == 2
    assert second["coverage"]["places"] > first["coverage"]["places"]


def test_resume_unknown_run_is_a_user_error():
    run_cli("search", "--resume", "nope", expect=2)


def test_rank_reranks_without_new_searches():
    doc = sim_search("--depth", "quick")
    before = Ledger(ledger_path()).units("sim.search")
    ranked = jrun("rank", doc["run_id"], "--objective", "gems", "--min-rating", "4.5", "--top", "3")
    assert Ledger(ledger_path()).units("sim.search") == before
    assert ranked["objective"] == "gems" and len(ranked["results"]) <= 3
    assert all(r["rating"] >= 4.5 for r in ranked["results"])
    assert set(ranked) >= {"run_id", "filters", "objective", "matched", "results", "next"}
    md = run_cli("rank", doc["run_id"], "--format", "md").stdout
    assert "| # | Name" in md


def test_followups_for_a_run():
    doc = sim_search("--depth", "quick")
    out = jrun("followups", doc["run_id"])
    assert out["run_id"] == doc["run_id"] and "types" in out and "name_tokens" in out


def test_runs_lists_and_purge_all_empties(state):
    doc = sim_search("--depth", "quick")
    rows = jrun("runs")["runs"]
    assert rows[0]["run_id"] == doc["run_id"] and rows[0]["queries"] == ["bakery"]
    assert rows[0]["depth"] == "quick" and rows[0]["places"] > 0 and "age_days" in rows[0]
    out = jrun("purge", "--all")
    assert out["deleted"] == [doc["run_id"]]
    assert jrun("runs")["runs"] == [] and list_runs(state) == []


def test_purge_older_than_keeps_recent_runs(state):
    old, new = sim_search("--depth", "quick"), sim_search("--depth", "quick")
    age(state, old["run_id"], 10)
    out = jrun("purge", "--older-than", "5")
    assert out["deleted"] == [old["run_id"]]
    assert [r["run_id"] for r in jrun("runs")["runs"]] == [new["run_id"]]


def age(state, run_id, days):
    spec = state / "runs" / run_id / "spec.json"
    t = time.time() - days * 86400
    os.utime(spec, (t, t))


def test_search_purges_runs_past_retention_first(state):
    old = sim_search("--depth", "quick")
    age(state, old["run_id"], 40)
    result = run_cli("search", "-q", "bakery", "--provider", "sim", "--depth", "quick")
    assert old["run_id"] in result.stderr
    assert not (state / "runs" / old["run_id"]).exists()


def test_retention_env_is_honoured(state, monkeypatch):
    old = sim_search("--depth", "quick")
    age(state, old["run_id"], 3)
    monkeypatch.setenv("MAPS_RETENTION_DAYS", "2")
    sim_search("--depth", "quick")
    assert not (state / "runs" / old["run_id"]).exists()


def test_resume_does_not_extend_the_retention_clock(state):
    first = sim_search("--depth", "standard", "--max-tasks", "2")
    age(state, first["run_id"], 20)
    jrun("search", "--resume", first["run_id"])
    assert jrun("runs")["runs"][0]["age_days"] >= 19.9


# --- user errors ------------------------------------------------------------------------------


def test_max_usd_requires_quality():
    r = run_cli("search", "-q", "x", "--provider", "sim", "--max-usd", "1", expect=2)
    assert "--max-usd" in r.stderr and "quality" in r.stderr
    sim_search("--depth", "quick", "--cost", "quality", "--max-usd", "1")


def test_missing_location_for_a_real_provider_is_a_user_error():
    r = run_cli("search", "-q", "bakery", expect=2)
    assert "--area" in r.stderr and "--bbox" in r.stderr


def test_google_without_key_names_the_variable():
    r = run_cli("search", "-q", "bakery", "--bbox", "24.5,46.5,24.9,46.9", expect=2)
    assert "GOOGLE_MAPS_API_KEY" in r.stderr and r.stderr.count("\n") <= 2


def test_query_is_required_without_resume():
    run_cli("search", "--provider", "sim", expect=2)


def test_conflicting_or_malformed_locations_exit_2():
    run_cli("search", "-q", "x", "--provider", "sim", "--bbox", "1,2,3,4", "--near", "1,2", expect=2)
    run_cli("search", "-q", "x", "--provider", "sim", "--bbox", "nonsense", expect=2)
    run_cli("search", "-q", "x", "--provider", "sim", "--bbox", "5,2,3,4", expect=2)
    run_cli("search", "-q", "x", "--provider", "sim", "--near", "24.7", expect=2)


def test_near_and_radius_build_a_box(state):
    doc = jrun(
        "search",
        "-q",
        "bakery",
        "--provider",
        "sim",
        "--depth",
        "quick",
        "--near",
        "24.7,46.7",
        "--radius",
        "5000",
    )
    box = BBox.parse(RunStore.open(state, doc["run_id"]).spec["bbox"])
    assert 9000 < box.height_m < 11000


def test_bad_filters_and_unknown_run_exit_2():
    run_cli("rank", "no-such-run", expect=2)
    doc = sim_search("--depth", "quick")
    run_cli("rank", doc["run_id"], "--name-regex", "(", expect=2)
    run_cli("rank", doc["run_id"], "--objective", "bogus", expect=2)


# --- budget stop ------------------------------------------------------------------------------


class ExhaustedProvider:
    name = "google"

    def __init__(self, budget):
        self.budget = budget

    def available(self):
        return True

    def text_search(self, query, *, bbox, level=0, max_results=60):
        raise BudgetExceeded("google.text_search.enterprise: free allowance exhausted")


def test_budget_stop_exits_zero_and_says_why(monkeypatch):
    monkeypatch.setattr(cli, "build_providers", lambda names, budget, tier, bbox: [ExhaustedProvider(budget)])
    doc = jrun("search", "-q", "bakery", "--bbox", "24.5,46.5,24.9,46.9")
    assert doc["coverage"]["stopped"] and "exhausted" in doc["coverage"]["stopped"]
    assert doc["results"] == [] and any("--cost quality" in n or "--resume" in n for n in doc["next"])


# --- reviews ----------------------------------------------------------------------------------


def test_reviews_round_trip_fetch_search_integrity_contributors():
    run = sim_search("--depth", "standard")["run_id"]
    fetched = jrun("reviews", "fetch", run, "--provider", "sim", "--top", "20", "--limit", "10")
    assert len(fetched["places"]) == 20 and fetched["stopped"] is None
    assert all(p["reviews"] > 0 for p in fetched["places"])

    again = jrun("reviews", "fetch", run, "--provider", "sim", "--top", "20", "--limit", "10")
    assert {p["place_id"] for p in again["places"]}.isdisjoint({p["place_id"] for p in fetched["places"]})

    found = jrun("reviews", "search", run, "fresh bread", "--top", "5")
    assert found["reviews_indexed"] > 0 and found["places_with_reviews"] == 40
    assert "only reviews fetched into this run" in found["coverage_note"].lower()
    assert 0 < len(found["results"]) <= 5
    for r in found["results"]:
        assert "fresh" in r["text"].lower()
        for key in ("author", "url", "flag_url", "published", "rating", "language"):
            assert key in r

    by_place = jrun("reviews", "search", run, "fresh bread", "--by-place", "--top", "3")
    assert by_place["results"] and "n_matches" in by_place["results"][0]
    assert "best" in by_place["results"][0]

    low = jrun("reviews", "search", run, "bread", "--max-rating", "2", "--top", "50")
    assert all(r["rating"] <= 2 for r in low["results"])

    integrity = jrun("reviews", "integrity", run, "--top", "3")
    assert len(integrity["places"]) == 3
    assert {"signals", "flags", "confidence", "caveat", "name"} <= set(integrity["places"][0])
    pid = fetched["places"][0]["place_id"]
    one = jrun("reviews", "integrity", run, "--place", pid)
    assert [p["place_id"] for p in one["places"]] == [pid]
    run_cli("reviews", "integrity", run, "--place", "missing", expect=2)

    graph = jrun("reviews", "contributors", run)
    assert isinstance(graph["contributors"], list) and "overlap" not in graph
    liked = jrun("reviews", "contributors", run, "--liked", pid)
    assert isinstance(liked["overlap"], list)


def _reviewed_run(flag_counts):
    """A run with one place per entry; entry i gets reviews whose repeated text raises flags."""
    from maps.models import Review

    run_id = sim_search("--depth", "quick")["run_id"]
    store = RunStore.open(cli.config.state_dir(), run_id)
    ids = list(store.places())[: len(flag_counts)]
    for pid, n_dupes in zip(ids, flag_counts, strict=True):
        revs = [
            Review(
                place_id=pid, text="same copied text here", rating=5.0, author=f"a{i}", author_id=f"u{pid}{i}"
            )
            for i in range(n_dupes)
        ]
        revs.append(
            Review(place_id=pid, text=f"unique words {pid}", rating=4.0, author="z", author_id=f"z{pid}")
        )
        store.add_reviews(pid, revs)
    return run_id, ids


def test_integrity_defaults_to_every_place_with_fetched_reviews():
    run, ids = _reviewed_run([0, 1, 2, 3, 4, 5])
    doc = jrun("reviews", "integrity", run)
    assert doc["places_checked"] == 6
    assert {p["place_id"] for p in doc["places"]} == set(ids)


def test_integrity_top_keeps_the_first_n_of_the_flag_order():
    run, _ = _reviewed_run([0, 1, 2, 3, 4, 5])
    doc = jrun("reviews", "integrity", run, "--top", "2")
    assert doc["places_checked"] == 2 and len(doc["places"]) == 2


def test_integrity_orders_by_flag_count_then_name():
    run, _ = _reviewed_run([0, 6, 0, 6, 0, 6])
    doc = jrun("reviews", "integrity", run)
    keys = [(-len(p["flags"]), p["name"]) for p in doc["places"]]
    assert keys == sorted(keys)
    assert len(doc["places"][0]["flags"]) > len(doc["places"][-1]["flags"])


def test_reviews_search_on_a_run_with_no_reviews_says_so():
    run = sim_search("--depth", "quick")["run_id"]
    out = jrun("reviews", "search", run, "bread")
    assert out["reviews_indexed"] == 0 and out["results"] == []


def test_reviews_fetch_stops_cleanly_on_budget(monkeypatch):
    run = sim_search("--depth", "quick")["run_id"]

    class Broke:
        name = "google"

        def available(self):
            return True

        def reviews(self, place_id, *, limit=5, language=None):
            raise BudgetExceeded("google.details.atmosphere: exhausted")

    monkeypatch.setattr(cli, "build_providers", lambda names, budget, tier, bbox: [Broke()])
    doc = jrun("reviews", "fetch", run)
    assert doc["places"] == [] and "exhausted" in doc["stopped"]


# --- google-only commands, with fakes -----------------------------------------------------------


class FakeGoogle:
    name = "google"

    def __init__(self):
        self.resolved: list[str] = []
        self.detailed: list[str] = []

    def available(self):
        return True

    def resolve_area(self, name):
        self.resolved.append(name)
        return BBox(24.5, 46.5, 24.9, 46.9), f"{name}, Saudi Arabia"

    def details(self, place_id, *, tier="enterprise", language=None):
        self.detailed.append(place_id)
        return Place(place_id, name=f"Name {place_id}", rating=4.4, review_count=30, primary_type="bakery")


class FakeAggregate:
    name = "google_aggregate"

    def available(self):
        return True

    def count(self, bbox, *, types, min_rating=None, max_rating=None, operational_only=True):
        self.last = (types, min_rating, max_rating)
        return 42

    def enumerate(self, bbox, *, types, min_rating=None, max_rating=None, operational_only=True, max_level=8):
        return EnumerationResult(place_ids=["g1", "g2", "g3"], total_count=3, requests=2)


@pytest.fixture
def fakes(monkeypatch):
    google, agg = FakeGoogle(), FakeAggregate()

    def build(names, budget, tier, bbox):
        return [{"google": google, "google_aggregate": agg}[n] for n in names]

    monkeypatch.setattr(cli, "build_providers", build)
    return google, agg


def test_area_resolves_once_then_uses_the_cache(fakes, state):
    google, _ = fakes
    first = jrun("area", "Riyadh")
    assert first["name"] == "Riyadh" and first["label"] == "Riyadh, Saudi Arabia"
    assert first["bbox"] == "24.50000,46.50000,24.90000,46.90000"
    second = jrun("area", "riyadh")
    assert second["bbox"] == first["bbox"] and google.resolved == ["Riyadh"]
    cache = json.loads((state / "areas.json").read_text())
    assert next(iter(cache.values()))["ts"] > 0


def test_stale_area_cache_is_refreshed(fakes, state):
    google, _ = fakes
    jrun("area", "Riyadh")
    cache = json.loads((state / "areas.json").read_text())
    for v in cache.values():
        v["ts"] -= 31 * 86400
    (state / "areas.json").write_text(json.dumps(cache))
    jrun("area", "Riyadh")
    assert google.resolved == ["Riyadh", "Riyadh"]


def test_census_counts_with_rating_bounds(fakes):
    _, agg = fakes
    doc = jrun(
        "census", "--bbox", "24.5,46.5,24.9,46.9", "--type", "bakery", "--type", "cafe", "--max-rating", "3.5"
    )
    assert doc["count"] == 42 and doc["filter"]["types"] == ["bakery", "cafe"]
    assert doc["filter"]["max_rating"] == 3.5 and agg.last == (["bakery", "cafe"], None, 3.5)
    run_cli("census", "--bbox", "24.5,46.5,24.9,46.9", expect=2)  # --type is required


def test_enumerate_records_id_only_places_in_a_new_or_existing_run(fakes, state):
    doc = jrun("enumerate", "--bbox", "24.5,46.5,24.9,46.9", "--type", "bakery", "--max-rating", "3.5")
    assert doc["total_count"] == 3 and doc["ids_found"] == 3 and doc["requests"] == 2
    assert doc["unresolved"] == [] and doc["stopped"] is None
    run = RunStore.open(state, doc["run_id"])
    places = run.places()
    assert set(places) == {"g1", "g2", "g3"}
    hit = places["g1"].hits[0]
    assert hit.provider == "google_aggregate" and hit.query == "type:bakery"
    key = "google_aggregate|bakery|24.50000,46.50000,24.90000,46.90000||3.5"
    assert run.done(key) is not None

    into = jrun("enumerate", "--bbox", "24.5,46.5,24.9,46.9", "--type", "cafe", "--into", doc["run_id"])
    assert into["run_id"] == doc["run_id"]
    assert jrun("runs")["runs"][0]["places"] == 3


def test_details_fills_names_for_missing_and_top(fakes, state):
    google, _ = fakes
    run = jrun("enumerate", "--bbox", "24.5,46.5,24.9,46.9", "--type", "bakery")["run_id"]
    doc = jrun("details", run, "--missing", "--tier", "pro")
    assert doc["fetched"] == 3 and doc["stopped"] is None and sorted(google.detailed) == ["g1", "g2", "g3"]
    places = RunStore.open(state, run).places()
    assert places["g1"].name == "Name g1" and places["g1"].rating == 4.4
    assert RunStore.open(state, run).done("google_details|g1|pro") is not None
    again = jrun("details", run, "--missing", "--tier", "pro")
    assert again["fetched"] == 0
    top = jrun("details", run, "--top", "2", "--tier", "enterprise")
    assert top["fetched"] == 2


def test_details_stops_cleanly_on_budget(fakes):
    google, _ = fakes
    run = jrun("enumerate", "--bbox", "24.5,46.5,24.9,46.9", "--type", "bakery")["run_id"]

    def broke(place_id, *, tier="enterprise", language=None):
        raise BudgetExceeded("google.details.enterprise: exhausted")

    google.details = broke
    doc = jrun("details", run, "--missing")
    assert doc["fetched"] == 0 and "exhausted" in doc["stopped"]


def test_area_flag_resolves_through_the_cache_and_searches(monkeypatch, fakes):
    google, _ = fakes

    class Searcher:
        name = "google"

        def __init__(self, budget):
            self.budget = budget

        def available(self):
            return True

        def text_search(self, query, *, bbox, level=0, max_results=60):
            return SearchPage([Place("x1", name="X", rating=4.5, review_count=3)], saturated=False)

    def build(names, budget, tier, bbox):
        # Area resolution asks for the provider without a box; the search itself always has one.
        return [google] if bbox is None else [Searcher(budget)]

    monkeypatch.setattr(cli, "build_providers", build)
    doc = jrun("search", "-q", "bakery", "--area", "Riyadh", "--depth", "quick")
    assert doc["matched"] == 1 and google.resolved == ["Riyadh"]
    jrun("search", "-q", "bakery", "--area", "Riyadh", "--depth", "quick")
    assert google.resolved == ["Riyadh"]  # second search reused the cached area


# --- eval -------------------------------------------------------------------------------------


def test_eval_sim_prints_the_benchmark_rows():
    doc = jrun("eval", "sim")
    assert [r["depth"] for r in doc["rows"]] == ["quick", "standard", "deep", "exhaustive"]
    assert doc["rows"][-1]["recall"] >= 0.97


def test_eval_check_scores_a_run_against_expected_ids(state):
    doc = sim_search("--depth", "quick", "--top", "3")
    ids = [r["place_id"] for r in doc["results"]]
    f = state / "expect.json"
    f.write_text(json.dumps({"name": "demo", "expected_place_ids": [*ids, "sim_nope"]}))
    out = jrun("eval", "check", doc["run_id"], "--expect", str(f))
    assert out["name"] == "demo" and out["expected"] == 4 and out["found"] == 3
    assert out["missing"] == ["sim_nope"] and out["recall"] == 0.75
    bad = state / "bad.json"
    bad.write_text("{not json")
    run_cli("eval", "check", doc["run_id"], "--expect", str(bad), expect=2)


@pytest.mark.parametrize("bad", ["nan", "inf", "-1"])
def test_max_usd_must_be_finite_and_non_negative(bad):
    r = run_cli("search", "-q", "x", "--provider", "sim", "--cost", "quality", "--max-usd", bad, expect=2)
    assert "--max-usd" in r.stderr


def test_resume_with_a_different_tier_is_refused_and_defaults_to_the_runs_tier():
    first = sim_search("--tier", "pro", "--depth", "quick")
    run_cli("search", "--resume", first["run_id"], "--tier", "ids", expect=2)
    assert jrun("search", "--resume", first["run_id"], "--tier", "pro")["run_id"] == first["run_id"]
    assert RunStore.open(state_dir(), first["run_id"]).spec["tier"] == "pro"
    assert jrun("search", "--resume", first["run_id"])["coverage"]["searches_run"] == 0


def test_resume_adds_queries_and_describes_the_whole_run():
    first = sim_search("--depth", "quick", q=("bakery",))
    rid = first["run_id"]
    second = jrun("search", "--resume", rid, "-q", "مخبز:ar")
    listed = {r["run_id"]: r for r in jrun("runs")["runs"]}[rid]
    assert listed["queries"] == ["bakery", "مخبز:ar"]
    assert second["coverage"]["places"] == listed["places"] > first["coverage"]["places"]
    assert second["coverage"]["searches_resumed"] > 0
    third = jrun("search", "--resume", rid)
    assert third["coverage"]["searches_run"] == 0 and third["coverage"]["places"] == listed["places"]
    assert jrun("followups", rid)["queries"] == ["bakery", "مخبز:ar"]
    # repeating an existing query does not duplicate it
    jrun("search", "--resume", rid, "-q", "bakery")
    assert {r["run_id"]: r for r in jrun("runs")["runs"]}[rid]["queries"] == ["bakery", "مخبز:ar"]


def test_resume_updates_depth_and_adds_providers():
    first = sim_search("--depth", "quick")
    jrun("search", "--resume", first["run_id"], "--depth", "standard")
    assert {r["run_id"]: r for r in jrun("runs")["runs"]}[first["run_id"]]["depth"] == "standard"
    spec = RunStore.open(state_dir(), first["run_id"]).spec
    assert spec["providers"] == ["sim"]


def test_reviews_fetch_stops_cleanly_when_the_provider_quota_is_reached(monkeypatch):
    run = sim_search("--depth", "quick")["run_id"]
    calls = []

    class QuotaOnThird:
        name = "google"

        def available(self):
            return True

        def reviews(self, place_id, *, limit=5, language=None):
            calls.append(place_id)
            if len(calls) == 3:
                raise QuotaExceeded("google: quota reached; `maps search --resume RUN` continues later")
            return [Review(place_id=place_id, text="nice", rating=5.0)]

    monkeypatch.setattr(cli, "build_providers", lambda names, budget, tier, bbox: [QuotaOnThird()])
    doc = jrun("reviews", "fetch", run)
    assert len(doc["places"]) == 2 and "quota" in doc["stopped"] and doc["errors"] == []
    stored = [p for p in RunStore.open(cli.config.state_dir(), run).places().values() if p.reviews]
    assert len(stored) == 2


def test_details_stops_cleanly_when_the_provider_quota_is_reached(fakes):
    google, _ = fakes
    run = jrun("enumerate", "--bbox", "24.5,46.5,24.9,46.9", "--type", "bakery")["run_id"]

    def quota(place_id, *, tier="enterprise", language=None):
        raise QuotaExceeded("google: quota reached")

    google.details = quota
    doc = jrun("details", run, "--missing", "--tier", "pro")
    assert doc["fetched"] == 0 and "quota" in doc["stopped"] and doc["errors"] == []


# --- doctor features / help -------------------------------------------------------------------


def test_doctor_reports_optional_features_and_the_agent_tools_provider():
    doc = jrun("doctor")
    assert doc["providers"]["google_agent_tools"]["available"] is False
    assert doc["features"]["semantic"]["install"] == "uv sync --extra semantic"
    assert doc["features"]["mcp"]["install"] == "uv sync --extra mcp"
    assert isinstance(doc["features"]["semantic"]["installed"], bool)
    assert isinstance(doc["features"]["mcp"]["installed"], bool)


def test_doctor_features_follow_importability(monkeypatch):
    monkeypatch.setattr(cli, "_importable", lambda name: name == "fastembed")
    feats = jrun("doctor")["features"]
    assert feats["semantic"]["installed"] is True and feats["mcp"]["installed"] is False


def test_top_level_help_mentions_discover_and_hybrid_search():
    out = run_cli("--help").stdout
    assert "maps discover" in out and "--mode hybrid" in out
    assert "discover" in out


# --- discover ---------------------------------------------------------------------------------

NOTE = "NOTE-SECRET-sourdough-crumb-text"
SUMMARY = "SUMMARY-SECRET: Loaf is a quiet sourdough bakery [0]."
ATTR_TITLE = "ATTR-TITLE-SECRET Google Maps"


class FakeAgentTools:
    name = "google_agent_tools"

    def __init__(self, raises=None, key=True):
        self.calls: list[tuple] = []
        self.raises = raises
        self.key = key

    def available(self):
        return self.key

    def search_places(self, text, *, bbox=None, language=None):
        self.calls.append((text, bbox, language))
        if self.raises:
            raise self.raises
        places = [
            Place(
                f"ChIJ_agent_{i}_abcdef",
                name=f"Loaf {i}",
                maps_url=f"https://maps.google.com/?cid={i}",
                hits=[Hit(provider="google_agent_tools", query=text, rank=i)],
            )
            for i in (1, 2)
        ]
        return AgentSearchResult(
            places=places,
            summary=SUMMARY,
            attributions={
                "ChIJ_agent_1_abcdef": {"title": ATTR_TITLE, "url": "https://attr.example/1"},
            },
            notes={"ChIJ_agent_1_abcdef": NOTE},
        )


@pytest.fixture
def agent(monkeypatch, ai_host):
    fake = FakeAgentTools()
    monkeypatch.setattr(cli, "build_agent_tools", lambda budget: fake)
    return fake


def test_discover_creates_a_run_with_the_places(agent, state):
    doc = jrun(
        "discover", "quiet bakery known for sourdough", "--bbox", "24.5,46.5,24.9,46.9", "--lang", "en"
    )
    assert doc["query"] == "quiet bakery known for sourdough" and doc["found"] == 2
    assert [p["place_id"] for p in doc["places"]] == ["ChIJ_agent_1_abcdef", "ChIJ_agent_2_abcdef"]
    first = doc["places"][0]
    assert first["name"] == "Loaf 1" and first["maps_url"].startswith("https://maps.google.com")
    assert first["note"] == NOTE and doc["places"][1]["note"] is None
    assert first["attribution"] == {"title": ATTR_TITLE, "url": "https://attr.example/1"}
    assert doc["places"][1]["attribution"] is None
    assert doc["summary"] == SUMMARY
    assert "Maps Experience agent tools" in doc["source_note"] and "Google Maps links" in doc["source_note"]
    assert "budget" in doc
    assert any("maps details" in n and "--missing" in n and doc["run_id"] in n for n in doc["next"])
    assert any(n.startswith(f"maps rank {doc['run_id']}") for n in doc["next"])
    text, bbox, lang = agent.calls[0]
    assert bbox is not None and lang == "en"
    run = RunStore.open(state, doc["run_id"])
    assert run.spec["discover"] == ["quiet bakery known for sourdough"]
    assert set(run.places()) == {"ChIJ_agent_1_abcdef", "ChIJ_agent_2_abcdef"}
    key = "google_agent_tools|quiet bakery known for sourdough|24.50000,46.50000,24.90000,46.90000"
    assert run.done(key) is not None and run.done(key).saturated is False


def test_discover_without_a_location_uses_a_dash_key(agent, state):
    doc = jrun("discover", "late night shawarma in Riyadh")
    assert agent.calls[0][1] is None
    run = RunStore.open(state, doc["run_id"])
    assert run.done("google_agent_tools|late night shawarma in Riyadh|-") is not None


def test_discover_into_adds_to_an_existing_run(agent, fakes_free, state):
    base = jrun("enumerate", "--bbox", "24.5,46.5,24.9,46.9", "--type", "bakery")
    doc = jrun("discover", "sourdough", "--into", base["run_id"])
    assert doc["run_id"] == base["run_id"]
    places = RunStore.open(state, base["run_id"]).places()
    assert {"g1", "g2", "g3", "ChIJ_agent_1_abcdef"} <= set(places)
    assert RunStore.open(state, base["run_id"]).spec["discover"] == ["sourdough"]


def test_discover_notes_summary_and_attribution_are_printed_but_never_stored(agent, state):
    doc = jrun("discover", "sourdough")
    printed = json.dumps(doc)
    secrets = (NOTE, SUMMARY, ATTR_TITLE)
    assert all(x in printed for x in secrets)
    for f in (state / "runs" / doc["run_id"]).rglob("*"):
        if f.is_file():
            assert not any(x in f.read_text(encoding="utf-8") for x in secrets)
    for f in state.rglob("*"):
        if f.is_file() and f.suffix in {".json", ".jsonl"}:
            assert not any(x in f.read_text(encoding="utf-8") for x in secrets)


def test_discover_without_a_key_names_the_variable(ai_host):
    result = run_cli("discover", "sourdough", expect=2)
    assert "GOOGLE_MAPS_API_KEY" in result.output


@pytest.mark.parametrize("err", [BudgetExceeded("google_agent_tools: refused"), QuotaExceeded("daily cap")])
def test_discover_stops_cleanly_on_budget_or_quota(monkeypatch, ai_host, err):
    monkeypatch.setattr(cli, "build_agent_tools", lambda budget: FakeAgentTools(raises=err))
    doc = jrun("discover", "sourdough")
    assert doc["stopped"] and str(err) in doc["stopped"]
    assert doc["found"] == 0 and doc["places"] == []


def test_discover_help_states_cost_and_status():
    out = run_cli("discover", "--help").stdout.lower()
    assert "experimental" in out and "unverified" in out and "free" in out and "sourdough" in out


@pytest.fixture
def fakes_free(monkeypatch):
    monkeypatch.setattr(
        cli, "build_providers", lambda names, budget, tier, bbox: [FakeAggregate() for _ in names]
    )


# --- reviews search modes ---------------------------------------------------------------------

CONCEPTS = [["grout", "joints", "crumbled"], ["late", "delayed"], ["coffee", "espresso"]]


def _vec(text):
    low = text.casefold()
    return [float(sum(low.count(w) for w in syns)) for syns in CONCEPTS]


class FakeEmbedder:
    def __init__(self, name="fake-v1"):
        self.name = name
        self.embedded = 0

    def embed(self, texts):
        self.embedded += len(texts)
        return [_vec(t) for t in texts]

    def embed_query(self, text):
        return _vec(text)


@pytest.fixture
def embedders(monkeypatch):
    made: list[FakeEmbedder] = []
    models: list[str] = []

    def build(model):
        models.append(model)
        made.append(FakeEmbedder(model))
        return made[-1]

    monkeypatch.setattr(cli, "build_embedder", build)
    return made, models


@pytest.fixture
def paraphrase_run(state):
    run_id = sim_search("--depth", "quick")["run_id"]
    store = RunStore.open(state, run_id)
    a, b, c = list(store.places())[:3]
    store.add_reviews(a, [Review(a, text="the tile grout crumbled after a week", rating=2.0, author="x")])
    store.add_reviews(
        b,
        [
            Review(b, text="grout is fine, joints neat", rating=5.0, author="y"),
            Review(b, text="lovely espresso", rating=4.0, author="y2"),
        ],
    )
    store.add_reviews(c, [Review(c, text="delayed again", rating=1.0, author="z")])
    return run_id, a, b, c


def test_keyword_mode_is_the_default_and_misses_the_paraphrase(paraphrase_run):
    run_id, a, b, _ = paraphrase_run
    doc = jrun("reviews", "search", run_id, "joints")
    assert doc["mode"] == "keyword" and "model" not in doc
    assert {r["place_id"] for r in doc["results"]} == {b}  # a's review never says "joints"


@pytest.mark.parametrize("mode", ["semantic", "hybrid"])
def test_semantic_and_hybrid_find_the_paraphrase(paraphrase_run, embedders, state, mode):
    run_id, a, b, _ = paraphrase_run
    doc = jrun("reviews", "search", run_id, "joints", "--mode", mode, "--top", "5")
    assert doc["mode"] == mode and doc["model"] == DEFAULT_MODEL
    assert {r["place_id"] for r in doc["results"][:2]} == {a, b}  # semantic also lists zero-score reviews
    assert (state / "runs" / run_id / "embeddings.json").exists()


def test_model_option_reaches_the_factory(paraphrase_run, embedders):
    run_id = paraphrase_run[0]
    doc = jrun("reviews", "search", run_id, "joints", "--mode", "semantic", "--model", "my/model")
    assert embedders[1] == ["my/model"] and doc["model"] == "my/model"


def test_second_semantic_call_embeds_nothing(paraphrase_run, embedders):
    run_id = paraphrase_run[0]
    jrun("reviews", "search", run_id, "joints", "--mode", "semantic")
    jrun("reviews", "search", run_id, "joints", "--mode", "hybrid")
    first, second = embedders[0]
    assert first.embedded == 4 and second.embedded == 0


def test_semantic_by_place_and_rating_filters(paraphrase_run, embedders):
    run_id, a, b, c = paraphrase_run
    doc = jrun("reviews", "search", run_id, "joints", "--mode", "semantic", "--by-place")
    assert {r["place_id"] for r in doc["results"][:2]} == {a, b}
    assert "n_matches" in doc["results"][0] and "best" in doc["results"][0]
    hyb = jrun("reviews", "search", run_id, "joints", "--mode", "hybrid", "--by-place", "--top", "1")
    assert len(hyb["results"]) == 1
    low = jrun("reviews", "search", run_id, "joints", "--mode", "semantic", "--max-rating", "2")
    assert {r["place_id"] for r in low["results"]} == {a, c}  # b's reviews are rated 4 and 5
    assert low["results"][0]["place_id"] == a
    high = jrun("reviews", "search", run_id, "joints", "--mode", "hybrid", "--min-rating", "4")
    assert {r["place_id"] for r in high["results"]} == {b}


def test_semantic_unavailable_exits_2_with_the_install_hint(paraphrase_run, monkeypatch):
    class Broken:
        name = "broken"

        def embed(self, texts):
            raise SemanticUnavailable(
                "Semantic search needs fastembed: install with `uv sync --extra semantic`."
            )

        def embed_query(self, text):
            raise AssertionError("unreachable")

    monkeypatch.setattr(cli, "build_embedder", lambda model: Broken())
    result = run_cli("reviews", "search", paraphrase_run[0], "joints", "--mode", "semantic", expect=2)
    assert "uv sync --extra semantic" in result.output


def test_reviews_search_help_explains_modes():
    out = run_cli("reviews", "search", "--help").stdout.lower()
    for word in ("keyword", "semantic", "hybrid", "locally", "download"):
        assert word in out


# --- reviews contributors --expand ------------------------------------------------------------

A, B, C = "1000000000001", "1000000000002", "1000000000003"


class FakeContributorProvider:
    name = "apify"

    def __init__(self, fail=None):
        self.calls: list[tuple[str, int]] = []
        self.fail = fail or {}

    def available(self):
        return True

    def contributor_reviews(self, contributor, *, limit=10):
        self.calls.append((contributor, limit))
        if contributor in self.fail:
            raise self.fail[contributor]
        out = []
        for i in (1, 2):
            pid = f"ChIJ_new_{contributor}_{i}"
            out.append(
                Place(
                    pid,
                    name=f"New {i}",
                    reviews=[
                        Review(
                            pid,
                            text="nice",
                            rating=5.0,
                            author_id=f"https://www.google.com/maps/contrib/{contributor}/reviews",
                            source="apify_contributor",
                        )
                    ],
                )
            )
        return out


@pytest.fixture
def contributor_run(state, approve_provider, reviewer_matching):
    approve_provider("apify_contributor")
    run_id = sim_search("--depth", "quick")["run_id"]
    store = RunStore.open(state, run_id)
    l1, l2 = list(store.places())[:2]

    def rev(pid, author, rating):
        return Review(pid, text=f"text by {author}", rating=rating, author_id=author, author="n")

    store.add_reviews(l1, [rev(l1, A, 5.0), rev(l1, B, 5.0), rev(l1, C, 3.0), rev(l1, "u-short", 5.0)])
    store.add_reviews(l2, [rev(l2, A, 4.5), rev(l2, C, 4.0)])
    return run_id, l1, l2


@pytest.fixture
def contributor_provider(monkeypatch):
    fake = FakeContributorProvider()
    monkeypatch.setattr(cli, "build_contributor_provider", lambda budget: fake)
    return fake


def test_expand_adds_places_and_reports_them(contributor_run, contributor_provider, state):
    run_id, l1, l2 = contributor_run
    doc = jrun(
        "reviews", "contributors", run_id, "--liked", l1, "--liked", l2, "--expand", "1", "--limit", "7"
    )
    assert doc["expanded"] == [{"author_id": A, "places_added": 2}]
    assert doc["new_places"] == 2 and doc["stopped"] is None and doc["errors"] == []
    assert "budget" in doc and "docs/permissions.md" in doc["privacy_note"]
    assert "public review histories" in doc["privacy_note"]
    assert contributor_provider.calls == [(A, 7)]
    assert f"ChIJ_new_{A}_1" in RunStore.open(state, run_id).places()
    assert RunStore.open(state, run_id).done(f"apify_contributor|{A}|7") is not None
    assert any(c["author_id"].endswith(A) for c in doc["contributors"])
    overlap_ids = {o["place_id"] for o in doc["overlap"]}
    assert {f"ChIJ_new_{A}_1", f"ChIJ_new_{A}_2"} <= overlap_ids  # recomputed after expansion


def test_expand_replays_for_free(contributor_run, contributor_provider):
    run_id, l1, _ = contributor_run
    jrun("reviews", "contributors", run_id, "--liked", l1, "--expand", "1")
    again = jrun("reviews", "contributors", run_id, "--liked", l1, "--expand", "1")
    assert len(contributor_provider.calls) == 1
    assert again["expanded"] == [] and again["new_places"] == 0


def test_expand_orders_by_liked_places_then_id_and_honours_n(contributor_run, contributor_provider):
    run_id, l1, l2 = contributor_run
    # A rated two liked places >= 4.0, B one, C one (its 3.0 on l1 does not count), "u-short" is no contributor.
    jrun("reviews", "contributors", run_id, "--liked", l1, "--liked", l2, "--expand", "3")
    assert [c for c, _ in contributor_provider.calls] == [A, B, C]
    assert all(limit == 10 for _, limit in contributor_provider.calls)


def test_expand_min_rating_changes_the_selection(contributor_run, contributor_provider):
    run_id, l1, l2 = contributor_run
    jrun(
        "reviews", "contributors", run_id, "--liked", l1, "--liked", l2, "--expand", "5", "--min-rating", "5"
    )
    assert [c for c, _ in contributor_provider.calls] == [A, B]  # A: only l1 at 5.0; ties by id


def test_expand_stops_on_a_budget_refusal_with_exit_0(contributor_run, monkeypatch):
    run_id, l1, _ = contributor_run
    fake = FakeContributorProvider(fail={A: BudgetExceeded("apify.contributor: free allowance exhausted")})
    monkeypatch.setattr(cli, "build_contributor_provider", lambda budget: fake)
    doc = jrun("reviews", "contributors", run_id, "--liked", l1, "--expand", "3")
    assert "exhausted" in doc["stopped"] and doc["expanded"] == [] and len(fake.calls) == 1


def test_expand_stops_on_quota_after_partial_progress(contributor_run, monkeypatch):
    run_id, l1, _ = contributor_run
    fake = FakeContributorProvider(fail={B: QuotaExceeded("monthly cap")})
    monkeypatch.setattr(cli, "build_contributor_provider", lambda budget: fake)
    doc = jrun("reviews", "contributors", run_id, "--liked", l1, "--expand", "3")
    assert [e["author_id"] for e in doc["expanded"]] == [A] and "monthly cap" in doc["stopped"]


def test_expand_continues_past_a_provider_error(contributor_run, monkeypatch):
    run_id, l1, _ = contributor_run
    fake = FakeContributorProvider(fail={A: ProviderError("apify: bad item")})
    monkeypatch.setattr(cli, "build_contributor_provider", lambda budget: fake)
    doc = jrun("reviews", "contributors", run_id, "--liked", l1, "--expand", "2")
    assert [e["author_id"] for e in doc["expanded"]] == [B]
    assert len(doc["errors"]) == 1 and A in doc["errors"][0] and doc["stopped"] is None


def test_expand_needs_liked_and_a_token(contributor_run):
    run_id, l1, _ = contributor_run
    run_cli("reviews", "contributors", run_id, "--expand", "2", expect=2)
    result = run_cli("reviews", "contributors", run_id, "--liked", l1, "--expand", "2", expect=2)
    assert "APIFY_TOKEN" in result.output


def test_contributors_help_states_cost():
    out = run_cli("reviews", "contributors", "--help").stdout.lower()
    assert "apify" in out and "unverified" in out and "--expand" in out


# --- account protection -----------------------------------------------------------------------

BOX = "24.5,46.5,24.9,46.9"
APPROVE_ARGS = (
    "--owner-statement",
    "I approve apify, I read its terms",
    "--data-rights",
    "apify terms allow it",
    "--retrieval-method",
    "public pages, no login",
    "--downstream-use",
    "personal notes",
)


class FakeSearcher:
    def __init__(self, name, stop=None):
        self.name, self.stop, self.calls, self.budget = name, stop, 0, None

    def available(self):
        return True

    def text_search(self, query, *, bbox, level=0, max_results=60, **kw):
        self.calls += 1
        if self.stop:
            raise self.stop
        pid = f"ChIJ_{self.name}_{query.text}_{self.calls}_abcdef"
        return SearchPage(
            places=[Place(pid, name="X", hits=[Hit(provider=self.name, query=query.text, rank=1)])]
        )


def use_searchers(monkeypatch, *searchers):
    by_name = {s.name: s for s in searchers}
    monkeypatch.setattr(cli, "build_providers", lambda names, budget, tier, bbox: [by_name[n] for n in names])


def refusal(*args):
    result = run_cli(*args, expect=2)
    assert result.stderr.startswith("Account protection:"), result.stderr
    assert len(result.stderr.strip().splitlines()) == 1
    return result.stderr


def test_apify_is_refused_until_the_owner_approves_it(monkeypatch, policy_dir):
    monkeypatch.setenv("APIFY_TOKEN", "tok")
    msg = refusal("search", "-q", "bakery", "--bbox", BOX, "--provider", "apify", "--cost", "quality")
    assert "maps policy approve apify --kind provider" in msg
    from maps import policy

    assert policy.approved("apify", "provider") is None
    jrun("policy", "approve", "apify", "--kind", "provider", *APPROVE_ARGS)
    assert policy.approved("apify", "provider")["subject"] == "apify"
    fake = FakeSearcher("apify")
    use_searchers(monkeypatch, fake)
    doc = jrun("search", "-q", "bakery", "--bbox", BOX, "--provider", "apify", "--depth", "quick")
    assert fake.calls == 1 and doc["coverage"]["places"] == 1


def test_dataforseo_is_gated_too(monkeypatch):
    monkeypatch.setenv("DATAFORSEO_LOGIN", "u")
    monkeypatch.setenv("DATAFORSEO_PASSWORD", "p")
    refusal("search", "-q", "x", "--bbox", BOX, "--provider", "dataforseo", "--cost", "quality")


def test_reviews_fetch_gates_the_provider(monkeypatch):
    run = sim_search("--depth", "quick")["run_id"]
    monkeypatch.setenv("APIFY_TOKEN", "tok")
    refusal("reviews", "fetch", run, "--provider", "apify")


def test_approve_with_a_missing_detail_exits_2(policy_dir):
    result = run_cli(
        "policy",
        "approve",
        "apify",
        "--kind",
        "provider",
        "--owner-statement",
        "ok",
        "--data-rights",
        "x",
        "--retrieval-method",
        "y",
        expect=2,
    )
    from maps import policy

    assert "downstream_use" in result.stderr and policy.approved("apify", "provider") is None
    run_cli(
        "policy", "approve", "apify", "--kind", "provider", "--data-rights", "x", expect=2
    )  # no statement
    run_cli("policy", "approve", "google", "--kind", "provider", *APPROVE_ARGS, expect=2)  # not conditional


def test_approve_and_clear_stop_help_say_they_record_the_owners_decision():
    for cmd in ("approve", "clear-stop"):
        out = " ".join(run_cli("policy", cmd, "--help").stdout.lower().split())
        assert "owner" in out and "verbatim" in out


def test_revoke_removes_an_approval(monkeypatch):
    jrun("policy", "approve", "apify", "--kind", "provider", *APPROVE_ARGS)
    jrun("policy", "revoke", "apify")
    monkeypatch.setenv("APIFY_TOKEN", "tok")
    refusal("search", "-q", "x", "--bbox", BOX, "--provider", "apify", "--cost", "quality")


def test_contributor_expansion_needs_its_own_approval(
    contributor_run, contributor_provider, approve_provider
):
    run_id, l1, _ = contributor_run
    from maps import policy

    policy.revoke("apify_contributor")
    approve_provider("apify")  # the other approval does not carry over
    msg = refusal("reviews", "contributors", run_id, "--liked", l1, "--expand", "1")
    assert "apify_contributor" in msg and contributor_provider.calls == []
    approve_provider("apify_contributor")
    jrun("reviews", "contributors", run_id, "--liked", l1, "--expand", "1")
    assert len(contributor_provider.calls) == 1


def test_a_cookie_bearing_contributor_env_template_is_ignored(contributor_run, approve_provider, monkeypatch):
    import httpx

    run_id, l1, _ = contributor_run
    approve_provider("apify_contributor")
    monkeypatch.setenv("APIFY_TOKEN", "tok")
    monkeypatch.setenv(
        "MAPS_APIFY_CONTRIBUTOR_INPUT", json.dumps({"cookies": "SID=abc", "u": "{contributor_url}"})
    )
    sent = []

    def fake_post(self, url, json=None, **k):
        sent.append(json)
        return httpx.Response(201, json=[], request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    run_cli("reviews", "contributors", run_id, "--liked", l1, "--expand", "1")
    assert sent and all("cookies" not in body and "SID=abc" not in str(body) for body in sent)


def test_a_personal_oauth_token_as_the_google_key_is_refused(monkeypatch):
    import httpx

    sent = []
    monkeypatch.setattr(httpx.Client, "request", lambda self, *a, **k: sent.append(a) or None)
    monkeypatch.setattr(httpx.Client, "post", lambda self, *a, **k: sent.append(a) or None)
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "ya29.personal")
    refusal("census", "--bbox", BOX, "--type", "bakery")
    refusal("search", "-q", "x", "--bbox", BOX, "--depth", "quick")
    assert sent == []


# discover --------------------------------------------------------------------------------------


def test_discover_needs_a_named_ai_host(agent, monkeypatch):
    monkeypatch.delenv("MAPS_AI_HOST")
    msg = refusal("discover", "sourdough")
    assert "MAPS_AI_HOST" in msg and agent.calls == []
    monkeypatch.setenv("MAPS_AI_HOST", "claude")
    msg = refusal("discover", "sourdough")
    assert "maps policy approve claude --kind ai_host" in msg and agent.calls == []
    jrun(
        "policy",
        "approve",
        "claude",
        "--kind",
        "ai_host",
        "--owner-statement",
        "ok",
        "--data-handling",
        "no training",
    )
    assert jrun("discover", "sourdough")["found"] == 2


def test_discover_via_gemini_uses_its_own_task_key(monkeypatch, state, ai_host):
    fake = FakeAgentTools()
    seen = []
    monkeypatch.setattr(cli, "build_discoverer", lambda via, budget: seen.append(via) or fake)
    doc = jrun("discover", "late shawarma", "--via", "gemini", "--bbox", BOX)
    assert seen == ["gemini"] and fake.calls[0][0] == "late shawarma"
    run = RunStore.open(state, doc["run_id"])
    assert run.done(f"gemini_maps|late shawarma|{BBox.parse(BOX).key()}") is not None
    assert run.spec["providers"] == ["gemini_maps"]
    doc = jrun("discover", "x", "--via", "gemini")
    assert RunStore.open(state, doc["run_id"]).done("gemini_maps|x|-") is not None


def test_discover_defaults_to_agent_tools(monkeypatch, ai_host):
    seen = []
    monkeypatch.setattr(cli, "build_discoverer", lambda via, budget: seen.append(via) or FakeAgentTools())
    jrun("discover", "x")
    assert seen == ["agent_tools"]


def test_discover_via_gemini_without_a_key_names_the_variable(monkeypatch, ai_host):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    result = run_cli("discover", "x", "--via", "gemini", expect=2)
    assert "GEMINI_API_KEY" in result.output


def test_discover_via_must_be_known(ai_host):
    run_cli("discover", "x", "--via", "chatgpt", expect=2)


# policy stops ------------------------------------------------------------------------------------


def test_a_policy_stop_ends_the_search_exits_3_and_blocks_the_provider(
    monkeypatch, approve_provider, policy_dir
):
    approve_provider("apify")
    apify = FakeSearcher("apify", stop=PolicyStop("apify", "HTTP 403: account suspended"))
    google = FakeSearcher("google")
    use_searchers(monkeypatch, apify, google)
    args = (
        "search",
        "-q",
        "bakery",
        "-q",
        "cafe",
        "--bbox",
        BOX,
        "--provider",
        "apify",
        "--provider",
        "google",
    )
    result = run_cli(*args, expect=3)
    doc = json.loads(result.stdout)
    stop = doc["policy_stop"]
    assert stop["provider"] == "apify" and "suspended" in stop["detail"]
    assert stop["action"].startswith("Stop. Do not retry or work around this.")
    assert "maps policy clear-stop" in stop["action"]
    assert doc["coverage"]["stopped"].startswith("POLICY STOP")
    assert (apify.calls, google.calls) == (1, 0)  # nothing was sent after the stop
    stops = json.loads((policy_dir / "stops.json").read_text())
    assert stops[0]["provider"] == "apify" and "suspended" in stops[0]["detail"]

    msg = refusal(*args)
    assert "maps policy clear-stop apify" in msg and apify.calls == 1
    jrun("policy", "clear-stop", "apify", "--owner-statement", "owner: account is fine again")
    apify.stop = None
    doc = jrun(*args)
    assert "policy_stop" not in doc and apify.calls > 1


def test_clear_stop_needs_a_statement_and_an_open_stop():
    run_cli("policy", "clear-stop", "apify", "--owner-statement", "ok", expect=2)
    run_cli("policy", "clear-stop", "apify", expect=2)


def test_a_policy_stop_in_discover_exits_3(monkeypatch, ai_host, policy_dir):
    stop = PolicyStop("google_agent_tools", "HTTP 403: suspended")
    monkeypatch.setattr(cli, "build_agent_tools", lambda budget: FakeAgentTools(raises=stop))
    doc = json.loads(run_cli("discover", "sourdough", expect=3).stdout)
    assert doc["policy_stop"]["provider"] == "google_agent_tools" and doc["found"] == 0
    assert json.loads((policy_dir / "stops.json").read_text())[0]["provider"] == "google_agent_tools"


def test_a_policy_stop_in_details_and_reviews_fetch_exits_3(monkeypatch, state, fakes):
    google, _ = fakes
    run = RunStore.create(state, {"queries": [], "bbox": BOX, "providers": ["google"], "tier": None})
    run.record("k", [Place("g1", hits=[Hit(provider="google", query="q", rank=1)])], saturated=False)

    def boom(*a, **k):
        raise PolicyStop("google", "HTTP 403: suspended")

    google.details = boom
    doc = json.loads(run_cli("details", run.run_id, "--missing", expect=3).stdout)
    assert doc["policy_stop"]["provider"] == "google"
    jrun("policy", "clear-stop", "google", "--owner-statement", "ok")
    google.reviews = boom
    doc = json.loads(run_cli("reviews", "fetch", run.run_id, expect=3).stdout)
    assert doc["policy_stop"]["provider"] == "google"


def test_a_policy_stop_in_census_area_enumerate_exits_3(monkeypatch, fakes, policy_dir):
    google, agg = fakes

    def boom(*a, **k):
        raise PolicyStop("google_aggregate", "HTTP 451")

    agg.count = boom
    doc = json.loads(run_cli("census", "--bbox", BOX, "--type", "bakery", expect=3).stdout)
    assert doc["policy_stop"]["provider"] == "google_aggregate"
    jrun("policy", "clear-stop", "google_aggregate", "--owner-statement", "ok")
    agg.enumerate = boom
    run_cli("enumerate", "--bbox", BOX, "--type", "bakery", expect=3)
    jrun("policy", "clear-stop", "google_aggregate", "--owner-statement", "ok")
    google.resolve_area = boom
    run_cli("area", "Riyadh", expect=3)


def test_a_policy_stop_in_contributor_expansion_exits_3(contributor_run, monkeypatch, approve_provider):
    run_id, l1, _ = contributor_run
    approve_provider("apify_contributor")
    fake = FakeContributorProvider(fail={A: PolicyStop("apify", "HTTP 403: banned")})
    monkeypatch.setattr(cli, "build_contributor_provider", lambda budget: fake)
    doc = json.loads(
        run_cli("reviews", "contributors", run_id, "--liked", l1, "--expand", "3", expect=3).stdout
    )
    assert doc["policy_stop"]["provider"] == "apify" and len(fake.calls) == 1
    refusal(
        "reviews", "contributors", run_id, "--liked", l1, "--expand", "3"
    )  # apify_contributor shares the stop


# retention, state dir, status ---------------------------------------------------------------------


def test_retention_is_clamped_with_one_warning(monkeypatch):
    monkeypatch.setenv("MAPS_RETENTION_DAYS", "90")
    result = run_cli("search", "-q", "bakery", "--provider", "sim", "--depth", "quick")
    assert result.stderr.count("capped at 30") == 1
    assert jrun("doctor")["retention_days"] == 30
    monkeypatch.setenv("MAPS_RETENTION_DAYS", "7")
    assert "capped" not in run_cli("runs").stderr


def test_a_state_dir_inside_the_repo_is_refused(monkeypatch):
    repo = cli.config.REPO_ROOT
    target = repo / "knowledge" / "zz-state-test"
    monkeypatch.setenv("MAPS_STATE_DIR", str(target))
    for cmd in (("runs",), ("budget",), ("search", "-q", "x", "--provider", "sim"), ("purge",)):
        msg = refusal(*cmd)
        assert "repository" in msg
    assert not target.exists()
    monkeypatch.setenv("MAPS_STATE_DIR", str(repo / ".state" / "t"))
    run_cli("runs")  # allowed: .state is git-ignored


def test_policy_status_is_free_and_reports_everything(monkeypatch, approve_provider):
    approve_provider("apify")
    monkeypatch.setenv("MAPS_AI_HOST", "claude")
    doc = jrun("policy", "status")
    assert doc["registry"]["core"] and "apify" in {a["subject"] for a in doc["approvals"]}
    assert doc["ai_host"] == {"env": "claude", "approved": False}
    assert doc["retention_cap_days"] == 30 and doc["state_dir"]["ok"] and doc["excluded_methods"]


def test_policy_status_still_works_with_a_bad_state_dir(monkeypatch):
    monkeypatch.setenv("MAPS_STATE_DIR", str(cli.config.REPO_ROOT / "docs"))
    assert jrun("policy", "status")["state_dir"]["ok"] is False


def test_doctor_reports_account_protection_and_provider_classes(approve_provider):
    approve_provider("apify")
    doc = jrun("doctor")
    assert doc["account_protection"]["registry"]["core"]
    assert doc["providers"]["google"]["class"] == "core"
    assert doc["providers"]["google"]["approval"] == "service_use"
    assert (
        doc["providers"]["apify"]["class"] == "conditional" and doc["providers"]["apify"]["approved"] is True
    )
    assert doc["providers"]["dataforseo"]["approved"] is False


# --- the AI-host gate holds on every path to grounded output ------------------------------------


def _discover_run(state, provider="google_agent_tools"):
    return RunStore.create(
        state,
        {
            "queries": [],
            "discover": ["sourdough"],
            "bbox": BOX,
            "depth": None,
            "providers": [provider],
            "cost": "free",
            "tier": None,
        },
    )


def _no_http(monkeypatch):
    import httpx

    sent = []
    monkeypatch.setattr(httpx.Client, "post", lambda self, *a, **k: sent.append(a) or None)
    monkeypatch.setattr(httpx.Client, "request", lambda self, *a, **k: sent.append(a) or None)
    return sent


@pytest.mark.parametrize("provider", ["google_agent_tools", "gemini_maps"])
def test_search_cannot_resume_a_discover_run_even_without_an_ai_host(state, monkeypatch, provider):
    run = _discover_run(state, provider)
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "AIzaFAKE")
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaFAKE")
    sent = _no_http(monkeypatch)
    result = run_cli("search", "--resume", run.run_id, "-q", "quiet bakery", "--depth", "quick", expect=2)
    assert "maps discover" in result.stderr and f"--into {run.run_id}" in result.stderr
    assert sent == []


def test_an_approved_ai_host_does_not_make_search_accept_a_discover_run(state, monkeypatch, ai_host):
    run = _discover_run(state)
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "AIzaFAKE")
    sent = _no_http(monkeypatch)
    result = run_cli("search", "--resume", run.run_id, "-q", "quiet bakery", "--depth", "quick", expect=2)
    assert "maps discover" in result.stderr and sent == []


@pytest.mark.parametrize("gate", ["build_providers", "_gated"])
@pytest.mark.parametrize("name", ["google_agent_tools", "gemini_maps"])
def test_the_provider_gate_requires_the_ai_host_for_grounded_providers(monkeypatch, gate, name):
    from maps import policy
    from maps.budget import Budget

    monkeypatch.delenv("MAPS_AI_HOST")
    budget = Budget(Ledger(":memory:"))
    with pytest.raises(policy.PolicyViolation, match="MAPS_AI_HOST"):
        getattr(cli, gate)([name], budget, None, None)
    monkeypatch.setattr(cli, "_construct_providers", lambda *a: ["constructed"])
    monkeypatch.setenv("MAPS_AI_HOST", "testhost")
    policy.approve("testhost", "ai_host", owner_statement="o", details={"data_handling": "d"})
    assert getattr(cli, gate)([name], budget, None, None) == ["constructed"]


def test_the_gate_holds_even_when_a_test_swaps_the_builder(monkeypatch):
    from maps import policy
    from maps.budget import Budget

    monkeypatch.setattr(cli, "build_providers", lambda *a: ["fake"])
    monkeypatch.delenv("MAPS_AI_HOST")
    with pytest.raises(policy.PolicyViolation, match="MAPS_AI_HOST"):
        cli._gated(["google_agent_tools"], Budget(Ledger(":memory:")), None, None)


def test_discover_via_gemini_suspension_exits_3_and_records_the_stop(monkeypatch, ai_host, policy_dir):
    import httpx

    monkeypatch.setenv("GEMINI_API_KEY", "AIzaFAKE")

    def post(self, url, json=None, headers=None, **k):
        return httpx.Response(
            403,
            json={"error": {"message": "Consumer 'project:1' has been suspended."}},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.Client, "post", post)
    result = run_cli(
        "discover", "sourdough", "--via", "gemini", "--cost", "quality", "--max-usd", "1", expect=3
    )
    doc = json.loads(result.stdout)
    assert doc["policy_stop"]["provider"] == "gemini_maps"
    stops = json.loads((policy_dir / "stops.json").read_text())
    assert stops[0]["provider"] == "gemini_maps" and stops[0]["open"]
    # the whole Google family is now blocked, not only gemini
    assert "stopped" in refusal("discover", "sourdough")


# --- expiry runs on every command that touches the state directory ------------------------------


def _expired_run(state, days=40):
    old = RunStore.create(state, {"queries": ["x"], "providers": ["sim"]})
    age(state, old.run_id, days)
    return old


def test_an_expired_run_is_gone_after_rank(state):
    fresh = sim_search("--depth", "quick")
    old = _expired_run(state)
    result = run_cli("rank", fresh["run_id"])
    assert not old.dir.exists() and old.run_id in result.stderr


def test_an_expired_run_is_gone_after_runs(state):
    old = _expired_run(state)
    assert jrun("runs")["runs"] == [] and not old.dir.exists()


def test_an_expired_run_is_gone_after_discover_even_when_refused(state):
    old = _expired_run(state)
    run_cli("discover", "sourdough", expect=2)  # no AI host: refused
    assert not old.dir.exists()


def test_an_expired_run_is_gone_after_discover_that_works(state, agent):
    old = _expired_run(state)
    jrun("discover", "sourdough", "--bbox", BOX)
    assert not old.dir.exists()


def test_an_expired_run_is_gone_after_reviews_search(state):
    fresh = sim_search("--depth", "quick")
    old = _expired_run(state)
    jrun("reviews", "search", fresh["run_id"], "slow")
    assert not old.dir.exists()


def test_an_expired_run_is_gone_after_budget(state):
    old = _expired_run(state)
    run_cli("budget")
    assert not old.dir.exists()


def test_a_40_day_old_area_cache_entry_is_removed(state):
    cache = state / "areas.json"
    cache.write_text(
        json.dumps(
            {
                "riyadh": {"label": "R", "bbox": BOX, "ts": time.time() - 40 * 86400},
                "jeddah": {"label": "J", "bbox": BOX, "ts": time.time() - 86400},
            }
        )
    )
    run_cli("runs")
    assert list(json.loads(cache.read_text())) == ["jeddah"]


def test_the_policy_command_does_not_touch_the_state_directory(state):
    old = _expired_run(state)
    run_cli("policy", "status")
    assert old.dir.exists()


# --- service-area businesses ----------------------------------------------------------------------


class KwSearcher(FakeSearcher):
    def __init__(self, name="google"):
        super().__init__(name)
        self.kwargs = []

    def text_search(self, query, *, bbox, level=0, max_results=60, **kw):
        self.kwargs.append(kw)
        return super().text_search(query, bbox=bbox, level=level, max_results=max_results, **kw)


def test_service_area_businesses_flag_reaches_the_provider_and_the_run_spec(monkeypatch, state):
    fake = KwSearcher()
    use_searchers(monkeypatch, fake)
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "AIzaFAKE")
    plain = jrun("search", "-q", "plumber", "--bbox", BOX, "--depth", "quick")
    assert fake.kwargs == [{}]
    assert (
        json.loads((state / "runs" / plain["run_id"] / "spec.json").read_text())["service_area_businesses"]
        is False
    )
    doc = jrun("search", "-q", "plumber", "--bbox", BOX, "--depth", "quick", "--service-area-businesses")
    assert fake.kwargs[-1] == {"include_service_area_businesses": True}
    spec = json.loads((state / "runs" / doc["run_id"] / "spec.json").read_text())
    assert spec["service_area_businesses"] is True
    keys = [
        json.loads(line)["key"]
        for line in (state / "runs" / doc["run_id"] / "tasks.jsonl").read_text().splitlines()
    ]
    assert all("include_service_area_businesses" in k for k in keys)


def test_service_area_businesses_is_honoured_on_resume(monkeypatch, state):
    fake = KwSearcher()
    use_searchers(monkeypatch, fake)
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "AIzaFAKE")
    first = jrun("search", "-q", "plumber", "--bbox", BOX, "--depth", "quick", "--service-area-businesses")
    jrun("search", "--resume", first["run_id"])
    assert fake.calls == 1  # replayed from the checkpoint: the key still matches
    jrun("search", "--resume", first["run_id"], "-q", "electrician")
    assert fake.kwargs[-1] == {"include_service_area_businesses": True}


def test_places_without_coordinates_are_kept_and_ranked(monkeypatch, state):
    class NoCoords(FakeSearcher):
        def text_search(self, query, *, bbox, level=0, max_results=60, **kw):
            places = [
                Place(
                    "ChIJ_nocoord_abcdef1",
                    name="Plumber One",
                    rating=4.5,
                    review_count=10,
                    hits=[Hit("google", query.text, rank=1)],
                ),
                Place(
                    "ChIJ_coord_abcdefg2",
                    name="Plumber Two",
                    lat=24.6,
                    lng=46.6,
                    rating=4.0,
                    review_count=5,
                    hits=[Hit("google", query.text, rank=2)],
                ),
            ]
            return SearchPage(places=places)

    use_searchers(monkeypatch, NoCoords("google"))
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "AIzaFAKE")
    doc = jrun("search", "-q", "plumber", "--bbox", BOX, "--depth", "quick", "--service-area-businesses")
    assert {r["name"] for r in doc["results"]} == {"Plumber One", "Plumber Two"}
    ranked = jrun("rank", doc["run_id"], "--objective", "gems")
    assert ranked["matched"] == 2
    md = run_cli("rank", doc["run_id"], "--format", "md").stdout
    assert "Plumber One" in md


# --- eval compare ---------------------------------------------------------------------------------


def _baseline_file(tmp_path, places, name="Owner's list", source="Maps by hand"):
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps({"name": name, "source": source, "places": places}))
    return path


def test_eval_compare_against_a_sim_run(state, tmp_path):
    run = sim_search("--depth", "quick", "--top", "100")
    rows = run["results"]
    best, second, last = rows[0], rows[1], rows[-1]
    baseline = _baseline_file(
        tmp_path,
        [
            {"place_id": best["place_id"]},
            {"name": f"  {second['name'].upper()} !!"},
            {"name": last["name"]},
            {"name": "A place that exists nowhere in the simulator"},
        ],
    )
    doc = jrun("eval", "compare", run["run_id"], "--baseline", baseline, "--top", "5")
    assert doc["baseline"] == {"name": "Owner's list", "source": "Maps by hand", "count": 4}
    overlap_ids = {o["place_id"] for o in doc["overlap"]}
    assert {best["place_id"], second["place_id"]} <= overlap_ids
    assert len(doc["only_toolkit"]) == 5 - len(doc["overlap"])
    assert all(r["rank"] <= 5 and "maps_url" in r for r in doc["only_toolkit"])
    gaps = {g["name"]: g["gap"] for g in doc["only_baseline"]}
    assert gaps["A place that exists nowhere in the simulator"] == "not retrieved"
    assert gaps[last["name"]].startswith("retrieved, ranked #")
    assert doc["counts"]["baseline"] == 4 and doc["counts"]["only_baseline"] == len(doc["only_baseline"])


def test_eval_compare_marks_filtered_out_places(state, tmp_path):
    run = sim_search("--depth", "quick", "--top", "100")
    low = min((r for r in run["results"] if r["rating"] is not None), key=lambda r: r["rating"])
    baseline = _baseline_file(tmp_path, [{"name": low["name"]}])
    doc = jrun("eval", "compare", run["run_id"], "--baseline", baseline, "--min-rating", "4.9", "--top", "3")
    assert doc["only_baseline"][0]["gap"] == "retrieved but filtered out"
    assert doc["filters"] and doc["counts"]["only_baseline"] == 1


def test_eval_compare_rejects_a_bad_baseline_and_an_unknown_run(state, tmp_path):
    run = sim_search("--depth", "quick")
    bad = _baseline_file(tmp_path, [{"name": "", "maps_url": "", "place_id": ""}])
    r = run_cli("eval", "compare", run["run_id"], "--baseline", bad, expect=2)
    assert "baseline" in r.stderr.lower()
    ok = _baseline_file(tmp_path, [{"name": "x"}])
    run_cli("eval", "compare", "nope", "--baseline", ok, expect=2)
