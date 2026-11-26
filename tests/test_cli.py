import json
import os
import time

import pytest
from click.testing import CliRunner

from maps import cli
from maps.budget import BudgetExceeded, Ledger
from maps.config import ledger_path
from maps.geography import BBox
from maps.models import Place
from maps.providers.base import SearchPage
from maps.providers.google_aggregate import EnumerationResult
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
    for cmd in ("doctor", "budget", "area", "search", "rank", "followups", "census", "enumerate",
                "details", "reviews", "runs", "purge", "eval"):
        assert cmd in out
    for cmd in ("search", "rank", "census", "details"):
        assert "cost" in run_cli(cmd, "--help").stdout.lower()


def test_doctor_shape_and_no_secret_values(monkeypatch):
    for k, v in SECRETS.items():
        monkeypatch.setenv(k, v)
    result = run_cli("doctor")
    doc = json.loads(result.stdout)
    assert set(doc) >= {"version", "state_dir", "retention_days", "providers", "prices_checked", "runs"}
    assert set(doc["providers"]) == {"google", "google_aggregate", "apify", "dataforseo", "sim"}
    assert doc["providers"]["google"] == {"available": True, "env": ["GOOGLE_MAPS_API_KEY"]}
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
    assert "sim.search" in skus and "google.text_search.enterprise" in skus
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
    doc = jrun("search", "-q", "bakery", "--provider", "sim", "--depth", "quick",
               "--near", "24.7,46.7", "--radius", "5000")
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
    doc = jrun("census", "--bbox", "24.5,46.5,24.9,46.9", "--type", "bakery", "--type", "cafe",
               "--max-rating", "3.5")
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
    assert RunStore.open(ledger_path().parent, first["run_id"]).spec["tier"] == "pro"
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
    spec = RunStore.open(ledger_path().parent, first["run_id"]).spec
    assert spec["providers"] == ["sim"]
