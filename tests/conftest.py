"""Shared fixtures: tests never touch the repo's real `policy/` folder."""

import pytest

from maps import config, policy


@pytest.fixture(autouse=True)
def policy_dir(tmp_path_factory, monkeypatch):
    path = tmp_path_factory.mktemp("policy")
    config.set_policy_dir_for_tests(path)
    monkeypatch.delenv("MAPS_AI_HOST", raising=False)
    yield path
    config.set_policy_dir_for_tests(None)


@pytest.fixture
def approve_provider():
    """Record the owner's approval of a conditional provider through the policy API."""

    def go(name):
        policy.approve(
            name,
            "provider",
            owner_statement="test owner approves",
            details={"data_rights": "dr", "retrieval_method": "rm", "downstream_use": "du"},
        )

    return go


@pytest.fixture
def ai_host(monkeypatch):
    """An AI host that is set and approved, as `maps discover` requires."""
    monkeypatch.setenv("MAPS_AI_HOST", "testhost")
    policy.approve(
        "testhost", "ai_host", owner_statement="test owner approves", details={"data_handling": "no training"}
    )
    return "testhost"


def _spend_details(billing, **kw):
    from datetime import date

    cap = {"daily_request_cap": kw["daily_request_cap"]} if "daily_request_cap" in kw else {}
    if billing == "none":
        return {"billing": "none", **cap}
    return {
        **cap,
        "billing": "enabled",
        "per_search_cap_usd": kw.get("per_search_cap_usd", 1000.0),
        "period_cap_usd": kw.get("period_cap_usd", 100000.0),
        "prices_checked_on": kw.get("prices_checked_on", date.today().isoformat()),
        "prices_source": kw.get("prices_source", "provider price page, read by the test"),
    }


@pytest.fixture
def grant_spend():
    """Record the owner's spending authorization for a billing subject (google, gemini, apify, dataforseo)."""

    def go(subject, billing="enabled", **kw):
        policy.approve(
            subject,
            "spend",
            owner_statement="test owner authorizes spending",
            details=_spend_details(billing, **kw),
        )

    return go


@pytest.fixture
def grant_service_use():
    """Record the owner's check of Google's terms for a core provider."""

    def go(*names):
        for name in names or ("google", "google_aggregate", "google_agent_tools", "gemini_maps"):
            policy.approve(
                name,
                "service_use",
                owner_statement="test owner approves",
                details={"terms_checked": "read the terms for this use"},
            )

    return go


@pytest.fixture
def spending_enabled(grant_spend):
    """Billing enabled with generous caps everywhere except Gemini (unconfirmed price: billing none)."""
    for subject in ("google", "apify", "dataforseo"):
        grant_spend(subject, "enabled")
    grant_spend("gemini", "none")


@pytest.fixture
def spending_none(grant_spend):
    """Google, Gemini and Apify declared as credentials that cannot be charged (free-mode rules still
    apply). DataForSEO has no free allowance, so under `none` every request would be out of pocket and
    refused: it is enabled with generous caps, for quality-mode budgets."""
    for subject in ("google", "gemini", "apify"):
        grant_spend(subject, "none")
    grant_spend("dataforseo", "enabled")


@pytest.fixture
def reviewer_matching():
    policy.approve(
        "reviewer_matching",
        "feature",
        owner_statement="test owner approves",
        details={"rights": "the owner may compare reviewers across places"},
    )


@pytest.fixture
def service_use_all(grant_service_use):
    """The terms of every core Google provider recorded as checked."""
    grant_service_use()
