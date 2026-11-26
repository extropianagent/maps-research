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
