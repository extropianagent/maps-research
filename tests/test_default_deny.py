"""Default-deny: every real provider needs a recorded approval, and no provider output reaches an AI
that is not recorded. The simulator needs nothing."""

from __future__ import annotations

import json

import httpx
import pytest
from click.testing import CliRunner

from maps import cli, config, policy
from maps.models import Hit, Place, Review
from maps.store import RunStore

CORE_GOOGLE = ("google", "google_aggregate", "google_agent_tools", "gemini_maps")
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


def make_run(state, *, hit="google", review_source=None):
    """A stored run, written by hand: places seen by `hit`, optionally with one review from `review_source`."""
    run = RunStore.create(state, {"queries": ["bakery"], "providers": [hit], "bbox": BOX, "depth": "quick"})
    places = [
        Place(
            f"ChIJplace{i:06d}",
            name=f"P{i}",
            rating=4.5,
            review_count=50,
            hits=[Hit(provider=hit, query="bakery")],
        )
        for i in range(3)
    ]
    run.record("k", places, saturated=False)
    if review_source:
        run.add_reviews(
            places[0].place_id,
            [
                Review(
                    place_id=places[0].place_id,
                    text="great",
                    rating=5,
                    author="A",
                    author_id="u1",
                    source=review_source,
                )
            ],
        )
    return run.run_id


# --- B1: Google's own providers need a recorded approval -------------------------------------------


@pytest.mark.parametrize("name", CORE_GOOGLE)
def test_each_core_google_provider_is_refused_without_service_use(name, grant_service_use):
    with pytest.raises(policy.PolicyViolation) as e:
        policy.require_provider(name)
    msg = str(e.value)
    assert (
        f"maps policy approve {name} --kind service_use --owner-statement" in msg and "--terms-checked" in msg
    )
    grant_service_use(name)
    policy.require_provider(name)


def test_a_service_use_record_covers_only_its_own_provider(grant_service_use):
    grant_service_use("google")
    policy.require_provider("google")
    with pytest.raises(policy.PolicyViolation, match="google_aggregate"):
        policy.require_provider("google_aggregate")


def test_sim_needs_no_record_and_conditional_providers_keep_their_own_kind(
    grant_service_use, approve_provider
):
    policy.require_provider("sim")
    grant_service_use("google")
    with pytest.raises(policy.PolicyViolation, match="--kind provider"):
        policy.require_provider("apify")
    approve_provider("apify")
    policy.require_provider("apify")


@pytest.mark.parametrize("terms", ["", "  ", None])
def test_service_use_needs_what_was_checked(terms):
    with pytest.raises(policy.PolicyViolation, match="terms_checked"):
        policy.approve("google", "service_use", owner_statement="s", details={"terms_checked": terms})


def test_an_incomplete_service_use_record_approves_nothing(policy_dir):
    (policy_dir / "approvals.json").write_text(
        json.dumps([{"subject": "google", "kind": "service_use", "owner_statement": "ok"}])
    )
    with pytest.raises(policy.PolicyViolation, match="incomplete"):
        policy.require_provider("google")


def test_service_use_applies_only_to_googles_providers():
    for subject in ("apify", "sim", "mystery"):
        with pytest.raises(policy.PolicyViolation):
            policy.approve(subject, "service_use", owner_statement="s", details={"terms_checked": "x"})


# --- B2: no provider output reaches an AI that is not recorded --------------------------------------


@pytest.fixture
def counting_google(monkeypatch, grant_service_use, spending_none):
    """Everything recorded except the AI host; a Google search would send a counted request."""
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "g-secret")
    grant_service_use("google", "google_aggregate")
    sent: list[httpx.Request] = []

    def handler(req):
        sent.append(req)
        return httpx.Response(200, json={"places": []})

    from maps.providers import google_aggregate, google_places

    for mod in (google_places, google_aggregate):
        monkeypatch.setattr(
            mod, "make_client", lambda timeout: httpx.Client(transport=httpx.MockTransport(handler))
        )
    return sent


def test_a_real_provider_search_without_an_ai_host_exits_2_before_any_request(counting_google):
    r = invoke("search", "-q", "bakery", "--bbox", BOX, "--depth", "quick")
    assert r.exit_code == 2
    assert r.stderr.startswith("Account protection:") and "MAPS_AI_HOST" in r.stderr
    assert counting_google == []


def test_an_ai_host_that_is_named_but_not_approved_is_refused(counting_google, monkeypatch):
    monkeypatch.setenv("MAPS_AI_HOST", "some-ai")
    r = invoke("search", "-q", "bakery", "--bbox", BOX, "--depth", "quick")
    assert r.exit_code == 2 and "maps policy approve some-ai --kind ai_host" in r.stderr
    assert counting_google == []


def test_with_an_approved_host_the_search_runs(counting_google, ai_host):
    run_cli("search", "-q", "bakery", "--bbox", BOX, "--depth", "quick")
    assert len(counting_google) == 1


def test_a_sim_search_works_with_no_records_at_all(policy_dir):
    doc = jrun("search", "-q", "bakery", "--provider", "sim", "--depth", "quick", "--top", "2")
    assert doc["results"]
    run_cli("reviews", "fetch", doc["run_id"], "--provider", "sim", "--top", "2")
    run_cli("rank", doc["run_id"])
    run_cli("followups", doc["run_id"])
    run_cli("reviews", "contributors", doc["run_id"])
    assert not list(policy_dir.glob("*"))  # the simulator leaves nothing in the policy folder


@pytest.mark.parametrize(
    "argv",
    [
        ("census", "--bbox", BOX, "--type", "bakery"),
        ("enumerate", "--bbox", BOX, "--type", "bakery"),
        ("area", "Riyadh"),
        ("search", "-q", "bakery", "--area", "Riyadh"),
    ],
)
def test_every_command_that_calls_a_provider_needs_the_host(counting_google, argv):
    r = invoke(*argv)
    assert r.exit_code == 2 and "MAPS_AI_HOST" in r.stderr, r.output
    assert counting_google == []


def test_a_cached_area_is_provider_output_too(counting_google, state):
    (state / "areas.json").write_text(
        json.dumps({"riyadh": {"label": "Riyadh", "bbox": BOX, "ts": __import__("time").time()}})
    )
    r = invoke("area", "Riyadh")
    assert r.exit_code == 2 and "MAPS_AI_HOST" in r.stderr


RUN_COMMANDS = {
    "rank": lambda rid, f: ("rank", rid),
    "followups": lambda rid, f: ("followups", rid),
    "reviews search": lambda rid, f: ("reviews", "search", rid, "great"),
    "reviews integrity": lambda rid, f: ("reviews", "integrity", rid),
    "reviews contributors": lambda rid, f: ("reviews", "contributors", rid),
    "eval check": lambda rid, f: ("eval", "check", rid, "--expect", f),
    "eval compare": lambda rid, f: ("eval", "compare", rid, "--baseline", f),
    "search resume": lambda rid, f: ("search", "--resume", rid),
    "details": lambda rid, f: ("details", rid, "--missing"),
    "reviews fetch": lambda rid, f: ("reviews", "fetch", rid, "--provider", "sim"),
}


@pytest.fixture
def expect_files(tmp_path):
    f = tmp_path / "expect.json"
    f.write_text(json.dumps({"name": "n", "expected_place_ids": ["ChIJplace000000"]}))
    g = tmp_path / "baseline.json"
    g.write_text(
        json.dumps({"name": "b", "source": "hand", "places": [{"name": "P0", "place_id": "ChIJplace000000"}]})
    )
    return {"eval check": str(f), "eval compare": str(g)}


@pytest.mark.parametrize("command", list(RUN_COMMANDS))
@pytest.mark.parametrize("kind", ["google sighting", "google review on a sim run"])
def test_a_run_with_provider_data_is_shown_only_to_an_approved_host(
    command, kind, state, expect_files, grant_service_use
):
    grant_service_use("google")
    rid = (
        make_run(state, hit="sim", review_source="google")
        if "review" in kind
        else make_run(state, hit="google")
    )
    argv = RUN_COMMANDS[command](rid, expect_files.get(command))
    r = invoke(*argv)
    assert r.exit_code == 2 and "MAPS_AI_HOST" in r.stderr, f"{command}: {r.output}"


@pytest.mark.parametrize(
    "command", ["rank", "followups", "reviews search", "reviews integrity", "eval check", "eval compare"]
)
def test_the_read_only_commands_work_once_the_host_is_approved(command, state, expect_files, ai_host):
    rid = make_run(state, hit="google", review_source="google")
    run_cli(*RUN_COMMANDS[command](rid, expect_files.get(command)))


def test_rank_on_google_sightings_is_refused_without_the_host_and_allowed_with_it(state, monkeypatch):
    rid = make_run(state, hit="google")
    r = invoke("rank", rid)
    assert r.exit_code == 2 and "MAPS_AI_HOST" in r.stderr
    monkeypatch.setenv("MAPS_AI_HOST", "testhost")
    policy.approve("testhost", "ai_host", owner_statement="s", details={"data_handling": "no training"})
    assert jrun("rank", rid)["results"]


@pytest.mark.parametrize(
    "command", ["rank", "followups", "reviews search", "reviews integrity", "eval check"]
)
def test_a_sim_only_run_is_exempt(command, state, expect_files):
    rid = make_run(state, hit="sim", review_source="sim")
    run_cli(*RUN_COMMANDS[command](rid, expect_files.get(command)))


def test_a_stored_sim_run_made_by_the_cli_is_exempt(policy_dir):
    doc = jrun("search", "-q", "bakery", "--provider", "sim", "--depth", "quick")
    jrun("reviews", "fetch", doc["run_id"], "--provider", "sim", "--top", "3")
    run_cli("reviews", "search", doc["run_id"], "fresh")
    run_cli("search", "--resume", doc["run_id"], "--depth", "quick")


# --- B3: reviewer matching is off until approved ----------------------------------------------------


def test_reviewer_matching_is_refused_on_a_non_sim_run_without_the_feature(state, ai_host):
    rid = make_run(state, hit="google", review_source="google")
    r = invoke("reviews", "contributors", rid)
    assert r.exit_code == 2 and r.stderr.startswith("Account protection:")
    assert (
        "maps policy approve reviewer_matching --kind feature --owner-statement" in r.stderr
        and "--rights" in r.stderr
    )
    r = invoke("reviews", "contributors", rid, "--liked", "ChIJplace000000")
    assert r.exit_code == 2 and "reviewer matching" in r.stderr


def test_reviewer_matching_runs_once_the_feature_is_approved(state, ai_host, reviewer_matching):
    rid = make_run(state, hit="google", review_source="google")
    assert "contributors" in jrun("reviews", "contributors", rid)


def test_the_feature_needs_rights_and_only_exists_for_reviewer_matching():
    with pytest.raises(policy.PolicyViolation, match="rights"):
        policy.approve("reviewer_matching", "feature", owner_statement="s", details={"rights": " "})
    with pytest.raises(policy.PolicyViolation, match="not a gated feature"):
        policy.approve("anything", "feature", owner_statement="s", details={"rights": "x"})


def test_the_host_is_still_checked_before_the_feature(state):
    rid = make_run(state, hit="google", review_source="google")
    r = invoke("reviews", "contributors", rid)
    assert r.exit_code == 2 and "MAPS_AI_HOST" in r.stderr


def test_a_sim_only_run_needs_no_feature(state):
    rid = make_run(state, hit="sim", review_source="sim")
    run_cli("reviews", "contributors", rid)
    run_cli("reviews", "contributors", rid, "--liked", "ChIJplace000000")


def test_expand_keeps_needing_apify_contributor_and_the_feature(
    state, ai_host, reviewer_matching, monkeypatch
):
    monkeypatch.setenv("APIFY_TOKEN", "t")
    rid = make_run(state, hit="google", review_source="google")
    r = invoke("reviews", "contributors", rid, "--liked", "ChIJplace000000", "--expand", "1")
    assert r.exit_code == 2 and "apify_contributor" in r.stderr
    # on a run with no non-sim review the feature is still needed to expand
    sim_rid = make_run(state, hit="sim", review_source="sim")
    policy.revoke("reviewer_matching")
    r = invoke("reviews", "contributors", sim_rid, "--liked", "ChIJplace000000", "--expand", "1")
    assert r.exit_code == 2 and "reviewer matching" in r.stderr


# --- B4: status and doctor ----------------------------------------------------------------------------


def test_status_and_doctor_show_per_provider_state(
    grant_service_use, grant_spend, approve_provider, monkeypatch
):
    monkeypatch.setenv("MAPS_AI_HOST", "testhost")
    for doc in (jrun("policy", "status"), jrun("doctor")["account_protection"]):
        p = doc["providers"]
        assert p["google"] == {
            "class": "core", "approval": "service_use", "approved": False, "spend_subject": "google", "spend_recorded": False,
        }  # fmt: skip
        assert p["sim"]["approved"] is True and p["sim"]["spend_subject"] is None
        assert p["apify"]["class"] == "conditional" and p["apify"]["approval"] == "provider"
        assert doc["ai_host"] == {"env": "testhost", "approved": False}
        assert doc["features"]["reviewer_matching"] == {"approved": False}
    grant_service_use("google", "gemini_maps")
    grant_spend("google", "none")
    approve_provider("apify")
    policy.approve("testhost", "ai_host", owner_statement="s", details={"data_handling": "none"})
    policy.approve("reviewer_matching", "feature", owner_statement="s", details={"rights": "r"})
    doc = jrun("policy", "status")
    p = doc["providers"]
    assert p["google"]["approved"] and p["google"]["spend_recorded"]
    assert (
        p["gemini_maps"]["approved"]
        and not p["gemini_maps"]["spend_recorded"]
        and p["gemini_maps"]["spend_subject"] == "gemini"
    )
    assert p["google_aggregate"]["spend_recorded"] and not p["google_aggregate"]["approved"]
    assert p["apify"]["approved"] and not p["apify"]["spend_recorded"]
    assert doc["ai_host"]["approved"] is True and doc["features"]["reviewer_matching"]["approved"] is True
    assert doc["kill_switch"]["on"] is False


def test_doctor_providers_show_class_approval_and_spend(grant_service_use, grant_spend):
    grant_service_use("google")
    grant_spend("google", "none")
    info = jrun("doctor")["providers"]
    assert (
        info["google"]["class"] == "core"
        and info["google"]["approved"] is True
        and info["google"]["spend_recorded"] is True
    )
    assert info["gemini_maps"]["approved"] is False and info["gemini_maps"]["spend_recorded"] is False
    assert info["apify"]["class"] == "conditional" and info["apify"]["approved"] is False
    assert config.policy_dir().exists()
