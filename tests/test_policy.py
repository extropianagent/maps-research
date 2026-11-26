import json

import pytest

from maps import config, policy
from maps.providers.base import PolicyStop, ProviderError

DETAILS = {"data_rights": "dr", "retrieval_method": "rm", "downstream_use": "du"}


def test_registry_contents():
    assert policy.CORE == {"google", "google_aggregate", "google_agent_tools", "gemini_maps", "sim"}
    assert policy.CONDITIONAL == {"apify", "dataforseo", "apify_contributor", "outscraper"}
    assert not policy.CORE & policy.CONDITIONAL
    text = " ".join(policy.EXCLUDED_METHODS).lower()
    for word in ("browser", "captcha", "proxy", "warehouse", "subscription"):
        assert word in text


def test_exceptions():
    assert issubclass(policy.PolicyViolation, RuntimeError)
    assert issubclass(PolicyStop, ProviderError)
    e = PolicyStop("apify", "account suspended")
    assert e.provider == "apify" and e.detail == "account suspended"


def test_policy_dir_env_and_default(monkeypatch, tmp_path):
    monkeypatch.setenv("MAPS_POLICY_DIR", str(tmp_path / "p"))
    assert config.policy_dir() == tmp_path / "p"
    monkeypatch.delenv("MAPS_POLICY_DIR")
    assert config.policy_dir() == config.REPO_ROOT / "policy"
    assert (config.REPO_ROOT / "maps").is_dir()


def test_approve_records_and_is_atomic_pretty_json(policy_dir):
    assert not (policy_dir / "approvals.json").exists()
    policy.approve("apify", "provider", owner_statement="yes go", details=DETAILS)
    rec = policy.approved("apify", "provider")
    assert rec["subject"] == "apify" and rec["kind"] == "provider"
    assert rec["owner_statement"] == "yes go" and rec["details"] == DETAILS
    assert len(rec["approved_on"]) == 10
    raw = (policy_dir / "approvals.json").read_text()
    assert json.loads(raw)[0]["subject"] == "apify" and "\n  " in raw
    assert policy.approved("apify", "ai_host") is None
    assert not list(policy_dir.glob("*.tmp"))


@pytest.mark.parametrize("missing", ["data_rights", "retrieval_method", "downstream_use"])
def test_provider_approval_needs_every_detail(missing):
    d = {**DETAILS, missing: "  "}
    with pytest.raises(policy.PolicyViolation, match=missing):
        policy.approve("apify", "provider", owner_statement="x", details=d)
    d.pop(missing)
    with pytest.raises(policy.PolicyViolation, match=missing):
        policy.approve("apify", "provider", owner_statement="x", details=d)


def test_approval_validation():
    with pytest.raises(policy.PolicyViolation):
        policy.approve("apify", "provider", owner_statement=" ", details=DETAILS)
    with pytest.raises(policy.PolicyViolation, match="conditional"):
        policy.approve("google", "provider", owner_statement="x", details=DETAILS)
    with pytest.raises(policy.PolicyViolation, match="data_handling"):
        policy.approve("claude", "ai_host", owner_statement="x", details={})
    with pytest.raises(policy.PolicyViolation):
        policy.approve("claude", "bogus", owner_statement="x", details={})
    policy.approve("claude", "ai_host", owner_statement="x", details={"data_handling": "no training"})
    assert policy.approved("claude", "ai_host")


def test_revoke():
    policy.approve("apify", "provider", owner_statement="x", details=DETAILS)
    policy.revoke("apify")
    assert policy.approved("apify", "provider") is None


def test_require_provider_rules(approve_provider):
    for name in policy.CORE:
        policy.require_provider(name)
    with pytest.raises(policy.PolicyViolation, match="not a registered provider.*maps/policy.py"):
        policy.require_provider("mystery")
    with pytest.raises(policy.PolicyViolation) as e:
        policy.require_provider("apify")
    msg = str(e.value)
    assert "maps policy approve apify --kind provider" in msg and "\n" not in msg
    for word in ("data rights", "retrieval method", "downstream use"):
        assert word in msg
    approve_provider("apify")
    policy.require_provider("apify")
    with pytest.raises(policy.PolicyViolation):
        policy.require_provider("apify_contributor")
    with pytest.raises(policy.PolicyViolation):
        policy.require_provider("outscraper")


def test_stops_block_until_cleared(approve_provider):
    approve_provider("apify")
    policy.record_stop("apify", "account suspended")
    assert policy.open_stops()[0]["provider"] == "apify"
    with pytest.raises(policy.PolicyViolation, match="maps policy clear-stop apify"):
        policy.require_provider("apify")
    with pytest.raises(policy.PolicyViolation):
        policy.clear_stop("apify", owner_statement=" ")
    policy.clear_stop("apify", owner_statement="owner: resolved")
    assert policy.open_stops() == []
    policy.require_provider("apify")


def test_a_stop_on_one_google_api_blocks_the_shared_key():
    policy.record_stop("google", "key suspended")
    with pytest.raises(policy.PolicyViolation):
        policy.require_provider("google_aggregate")
    policy.require_provider("sim")


def test_require_ai_host(monkeypatch):
    with pytest.raises(policy.PolicyViolation) as e:
        policy.require_ai_host()
    assert "MAPS_AI_HOST" in str(e.value) and "train" in str(e.value) and "ai_host" in str(e.value)
    monkeypatch.setenv("MAPS_AI_HOST", "claude")
    with pytest.raises(policy.PolicyViolation, match="maps policy approve claude --kind ai_host"):
        policy.require_ai_host()
    policy.approve("claude", "ai_host", owner_statement="x", details={"data_handling": "h"})
    assert policy.require_ai_host() == "claude"


@pytest.mark.parametrize(
    "payload",
    [
        {"Cookie": "x"},
        {"a": {"b": [{"sessionId": 1}]}},
        {"password": "p"},
        {"PASSWD": "p"},
        {"login": "l"},
        {"credentials": {}},
        {"proxyConfiguration": {"useApifyProxy": True}},
        {"solveCaptcha": True},
        {"note": "SID=abc"},
        {"x": ["HSID=1"]},
        {"x": "SSID=1"},
        {"x": "SAPISID=1"},
        {"x": "__Secure-1PSID=1"},
        {"x": "__Secure-3PSID=1"},
        {"x": "ya29.a0AfH6SMB"},
    ],
)
def test_outbound_payload_refused(payload):
    with pytest.raises(policy.PolicyViolation, match="Google credentials|circumvention"):
        policy.check_outbound_payload("apify", payload)


def test_outbound_payload_ordinary_passes():
    policy.check_outbound_payload(
        "apify",
        {
            "searchStringsArray": ["residence=nice bakery"],
            "startUrls": [{"url": "https://www.google.com/maps/contrib/1234567890123/reviews"}],
            "maxItems": 3,
            "customGeolocation": {"coordinates": [[[1.0, 2.0]]]},
        },
    )


def test_check_google_key():
    with pytest.raises(policy.PolicyViolation, match="dedicated"):
        policy.check_google_key("ya29.a0Af")
    for ok in ("AIzaSyXXXX", "", "SECRET-KEY-123", "placeholder"):
        policy.check_google_key(ok)


@pytest.mark.parametrize(
    "status,text",
    [
        (451, ""),
        (403, "Your project has been suspended"),
        (403, "This account is BANNED"),
        (429, "Automated queries / abuse detected"),
        (403, "We detected unusual traffic from your network"),
        (403, "Please solve the CAPTCHA"),
        (400, "This request violates our policy"),
        (403, "Violation of the Terms of Service"),
        (403, "see Acceptable Use rules"),
        (400, "terms of service"),
    ],
)
def test_detect_enforcement_positive(status, text):
    assert policy.detect_enforcement(status, text)


@pytest.mark.parametrize(
    "status,text",
    [
        (400, "Invalid argument: field mask is required"),
        (400, "API key not valid. Please pass a valid API key."),
        (403, "API key not valid. Please pass a valid API key."),
        (403, "Places API has not been used in project 123 before or it is disabled."),
        (403, "Requests to this API places.googleapis.com are blocked."),
        (403, "You must enable Billing on the Google Cloud Project"),
        (429, "Quota exceeded for quota metric 'Requests' and limit"),
        (429, ""),
        (500, "Internal error"),
        (503, "The service is currently unavailable."),
        (200, ""),
    ],
)
def test_detect_enforcement_negative(status, text):
    assert policy.detect_enforcement(status, text) is None


def test_state_dir_check(tmp_path):
    root = config.REPO_ROOT
    policy.state_dir_check(tmp_path)
    policy.state_dir_check(root / ".state")
    policy.state_dir_check(root / ".state" / "sub")
    with pytest.raises(policy.PolicyViolation, match="committed"):
        policy.state_dir_check(root / "knowledge")
    with pytest.raises(policy.PolicyViolation):
        policy.state_dir_check(root)


def test_retention_is_capped(monkeypatch):
    monkeypatch.setenv("MAPS_RETENTION_DAYS", "90")
    assert config.retention_days() == 30
    assert config.retention_clamped()
    monkeypatch.setenv("MAPS_RETENTION_DAYS", "7")
    assert config.retention_days() == 7 and not config.retention_clamped()
    monkeypatch.delenv("MAPS_RETENTION_DAYS")
    assert config.retention_days() == 30


def test_status(monkeypatch, tmp_path):
    monkeypatch.setenv("MAPS_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("MAPS_AI_HOST", "claude")
    policy.record_stop("apify", "x")
    st = policy.status()
    assert set(st) >= {
        "registry",
        "excluded_methods",
        "approvals",
        "open_stops",
        "ai_host",
        "retention_cap_days",
        "state_dir",
        "owner_responsibilities",
    }
    assert st["ai_host"] == {"env": "claude", "approved": False}
    assert st["retention_cap_days"] == 30 and st["state_dir"]["ok"] is True
    assert st["open_stops"][0]["provider"] == "apify"
    assert st["owner_responsibilities"] == [
        "Use a dedicated Google Cloud project with an API key restricted to the Maps APIs",
        "Check each AI host's data settings before recording it",
    ]
    assert st["registry"]["conditional"] == sorted(policy.CONDITIONAL)
