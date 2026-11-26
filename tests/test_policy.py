import json

import pytest

from maps import config, policy
from maps.providers.base import PolicyStop, ProviderError

DETAILS = {"data_rights": "dr", "retrieval_method": "rm", "downstream_use": "du"}


def test_registry_contents():
    assert policy.CORE == {"google", "google_aggregate", "google_agent_tools", "gemini_maps", "sim"}
    assert policy.CONDITIONAL == {"apify", "dataforseo", "apify_contributor", "outscraper", "hasdata"}
    assert not policy.CORE & policy.CONDITIONAL
    text = " ".join(policy.EXCLUDED_METHODS).lower()
    for word in ("browser", "captcha", "proxy", "warehouse", "subscription"):
        assert word in text


def test_exceptions():
    assert issubclass(policy.PolicyViolation, RuntimeError)
    assert issubclass(PolicyStop, ProviderError)
    e = PolicyStop("apify", "account suspended")
    assert e.provider == "apify" and e.detail == "account suspended"


def test_policy_dir_default_and_test_override(tmp_path):
    config.set_policy_dir_for_tests(None)
    assert config.policy_dir() == config.REPO_ROOT / "policy"
    assert (config.REPO_ROOT / "maps").is_dir()
    config.set_policy_dir_for_tests(tmp_path / "p")
    assert config.policy_dir() == tmp_path / "p"


def test_environment_variable_cannot_redirect_the_policy_folder(policy_dir, monkeypatch, tmp_path):
    policy.record_stop("google", "HTTP 403: project suspended")
    monkeypatch.setenv("MAPS_POLICY_DIR", str(tmp_path / "elsewhere"))
    assert config.policy_dir() == policy_dir
    with pytest.raises(policy.PolicyViolation, match="stopped"):
        policy.require_provider("google")


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


def test_require_provider_rules(approve_provider, service_use_all):
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


@pytest.mark.parametrize(
    "bad", [" ya29.abc", "Bearer ya29.abc", "YA29.abc", "bearer xyz", "  BEARER abc", "AIza-ok-ya29.inside"]
)
def test_check_google_key_refuses_tokens_in_any_disguise(bad):
    with pytest.raises(policy.PolicyViolation):
        policy.check_google_key(bad)


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


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("nan", 30), ("inf", 30), ("-inf", 30), ("-5", 30), ("0", 30), ("abc", 30), ("400", 30), ("7", 7)],
)
def test_retention_is_always_finite_and_in_zero_to_thirty(monkeypatch, raw, expected):
    import math

    monkeypatch.setenv("MAPS_RETENTION_DAYS", raw)
    days = config.retention_days()
    assert days == expected and math.isfinite(days) and 0 < days <= 30


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
        "Read a conditional provider's own terms before approving it; the record holds what you verified",
    ]
    assert st["registry"]["conditional"] == sorted(policy.CONDITIONAL)


@pytest.mark.parametrize(
    ("status", "text"),
    [
        (403, "Account suspension in effect"),
        (403, "Consumer 'project:1' has been suspended."),
        (403, "This account is under a ban"),
        (403, "Your IP has been banned"),
        (403, "Many requests were banned"),
        (403, "Your account has been terminated for misuse"),
        (403, "We've detected unusual activity on your account"),
        (403, "Unusual traffic from your network"),
        (403, "Automated queries detected"),
        (403, "Please solve the CAPTCHA"),
        (403, "Your account has been blocked"),
        (403, "Your account has been disabled by an administrator"),
        (403, "Violation of the Terms of Use"),
        (403, "You violated the terms of service"),
        (403, "Acceptable Use Policy breach"),
        (403, "Misuse of the service"),
        (429, "abuse of the service"),
        (451, ""),
    ],
)
def test_enforcement_wording_is_detected(status, text):
    assert policy.detect_enforcement(status, text)


@pytest.mark.parametrize(
    ("status", "text"),
    [
        (400, "API key not valid. Please pass a valid API key."),
        (
            403,
            "Places API (New) has not been used in project 123 before or it is disabled. Enable it by visiting https://console.developers.google.com/apis/api/places.googleapis.com/overview?project=123",
        ),
        (
            403,
            "Requests to this API places.googleapis.com method google.maps.places.v1.Places.SearchText are blocked.",
        ),
        (403, "This API method requires billing to be enabled."),
        (429, "Quota exceeded for quota metric 'Requests' and limit 'Requests per minute'"),
        (429, "Resource has been exhausted (e.g. check quota)."),
        (400, "Invalid JSON payload received."),
        (500, "Internal error"),
        (503, "The service is currently unavailable."),
        (403, "The account balance is low; top up to continue."),
    ],
)
def test_ordinary_errors_are_not_enforcement(status, text):
    assert policy.detect_enforcement(status, text) is None


@pytest.mark.parametrize(
    "payload",
    [
        {"headers": {"Authorization": "SAPISIDHASH 1700000000_abcdef"}},
        {"x": "SAPISIDHASH 1700000000_abcdef"},
        {"x": "__Secure-3PAPISID=abc"},
        {"x": "__Host-GAPS=abc"},
        {"x": "APISID=abc; NID=xyz"},
        {"x": "NID=xyz"},
        {"x": "LSID=abc"},
        {"x": "OSID=abc"},
        {"x": "__Secure-OSID=abc"},
        {"pwd": "hunter2"},
        {"pass": "hunter2"},
        {"pw": "x"},
        {"authToken": "1//0gAbCdEf-refresh"},
        {"auth_token": "x"},
        {"x": "1//0gAbCdEf-refresh"},
        {"googleAccount": {"email": "o@gmail.com", "pw": "x"}},
        {"account": "o@gmail.com"},
        {"x": "https://user:pw@proxy.example:8080"},
        {"x": "https://user:pw@example.com/a"},
        {"x": "socks5://10.0.0.1:1080"},
        {"x": "socks://10.0.0.1:1080"},
        {"x": "http://rotating-proxy.example:8000"},
        {"useResidentialIPs": True, "rotateIp": True},
        {"rotate": 1},
        {"proxies": []},
        {"Authorization": "Bearer abc"},
        {"captchaSolver": "x"},
    ],
)
def test_outbound_payload_defence_in_depth_refuses(payload):
    with pytest.raises(policy.PolicyViolation):
        policy.check_outbound_payload("apify", payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"passage": "x", "compass": 1, "accounting": 2, "tokenizer": "a"},
        {"searchStringsArray": ["quiet bakery, Riyadh"], "note": "1/2 of the places"},
        {"startUrls": [{"url": "https://www.google.com/maps/contrib/1234567890123/reviews"}], "maxItems": 5},
    ],
)
def test_outbound_payload_scan_has_no_false_positives_on_ordinary_words(payload):
    policy.check_outbound_payload("apify", payload)


def _bodies_sent_by_providers():
    import json as _json

    import httpx

    from maps.budget import Budget, Ledger
    from maps.geography import BBox
    from maps.models import Query
    from maps.providers.apify import ApifyProvider
    from maps.providers.dataforseo import DataForSEOProvider

    seen: list[tuple[str, object]] = []

    def apify_handler(req):
        seen.append(("apify", _json.loads(req.content)))
        return httpx.Response(201, json=[])

    def dfs_handler(req):
        body = _json.loads(req.content) if req.content else None
        seen.append(("dataforseo", body))
        status = {"id": "t1", "status_code": 20000, "status_message": "Ok.", "result": [{"items": []}]}
        return httpx.Response(200, json={"status_code": 20000, "tasks": [status]})

    budget = Budget(Ledger(":memory:"), mode="quality", max_usd=100, subjects=["dataforseo"])
    free = Budget(Ledger(":memory:"))  # apify is recorded as billing none: free-mode rules, inside its credit
    box = BBox(24.6, 46.6, 24.7, 46.7)
    a = ApifyProvider(free, token="t", client=httpx.Client(transport=httpx.MockTransport(apify_handler)))
    a.text_search(Query("bakery", "en"), bbox=box)
    a.reviews("ChIJplaceAAAA1", limit=5, language="en")
    a.contributor_reviews("1234567890123", limit=5)
    d = DataForSEOProvider(
        budget,
        login="l",
        password="p",
        client=httpx.Client(transport=httpx.MockTransport(dfs_handler)),
        sleep=lambda s: None,
    )
    d.text_search(Query("bakery", "en"), bbox=box)
    d.reviews("ChIJplaceAAAA1", limit=5, language="en")
    return seen


def test_the_toolkits_own_payloads_pass_the_scan(spending_none):
    seen = _bodies_sent_by_providers()
    kinds = {k for k, _ in seen}
    assert kinds == {"apify", "dataforseo"} and len(seen) >= 5
    for provider, body in seen:
        if body is not None:
            policy.check_outbound_payload(provider, body)


def _write_approvals(policy_dir, records):
    (policy_dir / "approvals.json").write_text(json.dumps(records))


def test_a_bare_subject_and_kind_record_is_not_an_approval(policy_dir):
    _write_approvals(policy_dir, [{"subject": "apify", "kind": "provider"}])
    assert policy.approved("apify", "provider") is None
    with pytest.raises(policy.PolicyViolation, match="incomplete.*maps policy approve apify"):
        policy.require_provider("apify")


def test_a_non_dict_record_never_crashes(policy_dir):
    _write_approvals(policy_dir, ["apify"])
    assert policy.approved("apify", "provider") is None
    with pytest.raises(policy.PolicyViolation):
        policy.require_provider("apify")
    assert policy.revoke("apify") == 0
    policy.approve("apify", "provider", owner_statement="ok", details=DETAILS)  # replaces the junk
    policy.require_provider("apify")


@pytest.mark.parametrize(
    "record",
    [
        {"subject": "apify", "kind": "provider", "owner_statement": "", "details": DETAILS},
        {"subject": "apify", "kind": "provider", "owner_statement": "ok", "details": {}},
        {
            "subject": "apify",
            "kind": "provider",
            "owner_statement": "ok",
            "details": {**DETAILS, "data_rights": " "},
        },
        {"subject": "apify", "kind": "provider", "owner_statement": "ok", "details": ["x"]},
        {"subject": "apify", "kind": "provider", "owner_statement": 5, "details": DETAILS},
    ],
)
def test_incomplete_provider_records_are_refused(policy_dir, record):
    _write_approvals(policy_dir, [record])
    with pytest.raises(policy.PolicyViolation, match="incomplete"):
        policy.require_provider("apify")


def test_incomplete_ai_host_record_is_refused(policy_dir, monkeypatch):
    monkeypatch.setenv("MAPS_AI_HOST", "h")
    _write_approvals(policy_dir, [{"subject": "h", "kind": "ai_host", "owner_statement": "ok"}])
    assert policy.approved("h", "ai_host") is None
    with pytest.raises(policy.PolicyViolation, match="incomplete"):
        policy.require_ai_host()


def test_status_lists_malformed_records(policy_dir):
    good = {"subject": "h", "kind": "ai_host", "owner_statement": "ok", "details": {"data_handling": "d"}}
    _write_approvals(policy_dir, [good, {"subject": "apify", "kind": "provider"}, "junk"])
    st = policy.status()
    assert [r["subject"] for r in st["approvals"]] == ["h"]
    assert len(st["invalid_approvals"]) == 2
    assert any("apify" in str(r) for r in st["invalid_approvals"])
