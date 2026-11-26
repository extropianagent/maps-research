"""Review round 5: the initialised marker is per billing subject; proxy variables in any case; a request
cannot outlive the run's time limit; a lease outlives its run; a dead run does not lock the owner out;
price freshness uses the billing time zone. Mock transports only; the policy folder is a temp dir."""

from __future__ import annotations

import json
import os

import pytest
from click.testing import CliRunner

from maps import budget as budget_mod
from maps import cli, config
from maps.budget import Budget, BudgetExceeded, GatewayRefusal, Ledger

GOOGLE_FREE = "google.text_search.ids"  # a google SKU with an unlimited free allowance
APIFY = "apify.place"


@pytest.fixture
def usage(policy_dir):
    return policy_dir / "usage.json"


def doc(usage):
    return json.loads(usage.read_text())


def file_budget(**kw):
    return Budget(Ledger(config.ledger_path()), **kw)


def invoke(*args):
    return CliRunner().invoke(cli.main, [str(a) for a in args])


def google_restore_args():
    args = ["budget", "restore", "--owner-statement", "console figures", "--subject", "google"]
    for n in Budget.restore_skus("google"):
        args += ["--sku", f"{n}=0"]
    return args


# --- 1: the initialised marker is per billing subject --------------------------------------------


def test_a_free_request_for_a_none_subject_does_not_initialise_an_enabled_subject(grant_spend, usage):
    grant_spend("google", "none")
    grant_spend("apify", "enabled", per_search_cap_usd=2.0, period_cap_usd=2.0)
    usage.unlink()  # the usage store was lost (fresh clone, ephemeral machine)
    b = file_budget()
    b.charge(GOOGLE_FREE)
    b.close()
    with pytest.raises(GatewayRefusal, match="maps budget restore"):
        file_budget(mode="quality", max_usd=2.0, subjects=["apify"]).preflight("apify")
    with pytest.raises(BudgetExceeded, match="maps budget restore"):
        file_budget(mode="quality", max_usd=2.0, subjects=["apify"]).charge(APIFY, 50)
    assert doc(usage)["initialized"] == {"google": budget_mod._today().isoformat()}


def test_restore_for_google_leaves_apify_uninitialised(grant_spend, usage):
    grant_spend("google", "enabled")
    grant_spend("apify", "enabled")
    usage.unlink()
    r = invoke(*google_restore_args())
    assert r.exit_code == 0, r.output
    assert set(doc(usage)["initialized"]) == {"google"}
    file_budget().charge(GOOGLE_FREE)
    with pytest.raises(BudgetExceeded, match="maps budget restore"):
        file_budget().charge(APIFY)


def test_approving_a_spend_record_for_google_does_not_initialise_apify(grant_spend, usage):
    grant_spend("apify", "enabled")
    usage.unlink()  # apify's usage was lost
    grant_spend("google", "enabled")
    assert set(doc(usage)["initialized"]) == {"google"}
    with pytest.raises(BudgetExceeded, match="maps budget restore"):
        file_budget().charge(APIFY)


def test_a_second_enabled_subject_approved_later_is_not_initialised(grant_spend, usage):
    grant_spend("google", "enabled")  # true first-time setup: no store anywhere
    assert set(doc(usage)["initialized"]) == {"google"}
    grant_spend("apify", "enabled")  # a store already exists: its figures must come from a restore
    assert set(doc(usage)["initialized"]) == {"google"}
    with pytest.raises(BudgetExceeded, match="maps budget restore"):
        file_budget().charge(APIFY)


def test_first_time_approval_of_an_enabled_subject_initialises_only_it(grant_spend, usage):
    assert not usage.exists()
    grant_spend("apify", "enabled")
    assert doc(usage)["initialized"] == {"apify": budget_mod._today().isoformat()}
    file_budget().charge(APIFY)


def test_approving_a_none_record_creates_no_store(grant_spend, usage):
    grant_spend("google", "none")
    assert not usage.exists()
    file_budget().charge(GOOGLE_FREE)  # first use initialises google for itself
    assert set(doc(usage)["initialized"]) == {"google"}


def test_an_old_store_wide_marker_initialises_no_subject(grant_spend, usage):
    grant_spend("google", "enabled")
    grant_spend("gemini", "none")
    usage.write_text(json.dumps({"initialized_on": "2026-01-01", "months": {}, "reconciliations": []}))
    with pytest.raises(BudgetExceeded, match="maps budget restore"):
        file_budget().charge(GOOGLE_FREE)
    with pytest.raises(GatewayRefusal, match="maps budget restore"):
        file_budget().preflight("google")
    file_budget().charge("google.gemini_maps_grounding")  # a none subject is initialised on first use
    assert set(doc(usage)["initialized"]) == {"gemini"}


# --- 2: proxy variables in any letter case -------------------------------------------------------

from maps import policy  # noqa: E402
from maps.providers import http as provider_http  # noqa: E402


@pytest.mark.parametrize(
    ("var", "value"),
    [
        ("Https_Proxy", "http://proxy.example.net:3128"),
        ("hTTP_PROXY", "http://203.0.113.9:8080"),
        ("ALL_Proxy", "socks5://10.0.0.5:1080"),
        ("hTTPs_pROXY", "http://proxy.example.net:3128"),
    ],
)
def test_a_proxy_variable_in_any_letter_case_is_refused_before_any_request(var, value, monkeypatch):
    for name in list(os.environ):
        if name.lower() in ("http_proxy", "https_proxy", "all_proxy"):
            monkeypatch.delenv(name)
    monkeypatch.setenv(var, value)
    with pytest.raises(policy.PolicyViolation, match="egress_proxy"):
        provider_http.make_client(timeout=5)


def test_loopback_proxy_values_in_mixed_case_variables_are_allowed(monkeypatch):
    for name in list(os.environ):
        if name.lower() in ("http_proxy", "https_proxy", "all_proxy"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("Https_Proxy", "http://127.0.0.1:3128")
    monkeypatch.setenv("ALL_Proxy", "http://localhost:1080")
    provider_http.make_client(timeout=5).close()


# --- 3: a request cannot outlive the run's time limit; a lease outlives its run -------------------

import httpx  # noqa: E402

from maps.geography import BBox  # noqa: E402
from maps.providers.apify import ApifyProvider  # noqa: E402
from maps.providers.google_aggregate import PlacesAggregate  # noqa: E402
from maps.providers.google_places import GooglePlaces  # noqa: E402

BOX = BBox(24.0, 46.0, 25.0, 47.0)


class FakeClock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


def seen_timeouts(build, call, at):
    """The timeouts of the requests a provider call sends when the run is `at` seconds old."""
    clock = FakeClock()
    seen: list[dict] = []

    def handler(req):
        seen.append(dict(req.extensions["timeout"]))
        return httpx.Response(200, json={})

    budget = Budget(Ledger(":memory:"), clock=clock, wall_clock=clock)
    provider = build(budget, httpx.Client(transport=httpx.MockTransport(handler)))
    clock.t = at
    try:
        call(provider)
    except Exception as e:  # noqa: BLE001 - only the request's timeout matters here
        err = e
    else:
        err = None
    assert seen, f"the provider sent no request: {err!r}"
    return seen


PROVIDER_CALLS = {
    "apify": (
        lambda b, c: ApifyProvider(b, token="t", client=c),
        lambda p: p.reviews("ChIJabcdefghij", limit=5),
        300.0,
    ),
    "google_places": (
        lambda b, c: GooglePlaces(b, api_key="k", client=c),
        lambda p: p.details("ChIJabcdefghij"),
        30.0,
    ),
    "google_aggregate": (
        lambda b, c: PlacesAggregate(b, api_key="k", client=c),
        lambda p: p.count(BOX, types=["cafe"]),
        60.0,
    ),
}


@pytest.mark.usefixtures("spending_enabled")
@pytest.mark.parametrize("name", sorted(PROVIDER_CALLS))
def test_a_request_late_in_the_run_gets_at_most_the_remaining_time(name):
    build, call, own = PROVIDER_CALLS[name]
    late = seen_timeouts(build, call, at=899.0)
    assert all(v <= 1.0 for t in late for v in t.values() if v is not None), late
    early = seen_timeouts(build, call, at=0.0)  # an early request keeps the provider's own timeout
    assert all(v == own for t in early for v in t.values() if v is not None), early


def test_the_request_timeout_helper_has_a_floor_of_one_second():
    clock = FakeClock()
    b = Budget(Ledger(":memory:"), clock=clock)
    clock.t = 899.9
    assert provider_http.request_timeout(b, 300.0) == 1.0
    clock.t = 5000.0  # past the ceiling: still a positive timeout
    assert provider_http.request_timeout(b, 300.0) == 1.0
    clock.t = 0.0
    assert provider_http.request_timeout(b, 30.0) == 30.0
    assert provider_http.request_timeout(b, 3000.0) == 900.0


@pytest.mark.usefixtures("spending_none")
def test_a_lease_outlives_its_run_by_the_largest_client_timeout():
    assert budget_mod.CLIENT_TIMEOUT_S == 300.0
    wall = FakeClock(1000.0)
    a = file_budget(wall_clock=wall)
    b = file_budget(wall_clock=wall)
    a.charge(GOOGLE_FREE)
    b.charge(GOOGLE_FREE)
    wall.t = 1000.0 + 1000.0  # past max_elapsed_s + grace, inside max_elapsed_s + client timeout + grace
    with pytest.raises(BudgetExceeded, match="runs are already active"):
        file_budget(wall_clock=wall).charge(GOOGLE_FREE)
    wall.t = 1000.0 + 900.0 + 300.0 + budget_mod.LEASE_GRACE_S + 1
    file_budget(wall_clock=wall).charge(GOOGLE_FREE)  # the full window has passed


# --- 4: a dead run does not lock the owner out ---------------------------------------------------

import fcntl  # noqa: E402
import logging  # noqa: E402
import socket  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402


def dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def write_leases(usage, *leases):
    usage.write_text(
        json.dumps(
            {
                "initialized": {"google": budget_mod._today().isoformat()},
                "months": {},
                "reconciliations": [],
                "leases": [
                    {
                        "id": f"run{i}",
                        "pid": pid,
                        "host": host,
                        "started": time.time() - age,
                        "max_elapsed_s": 900.0,
                    }
                    for i, (pid, host, age) in enumerate(leases)
                ],
            }
        )
    )


@pytest.mark.usefixtures("spending_none")
def test_leases_of_dead_runs_on_this_host_do_not_block_a_new_run(usage):
    gone, here = dead_pid(), socket.gethostname()
    write_leases(usage, (gone, here, 10.0), (gone, here, 20.0))
    file_budget().charge(GOOGLE_FREE)
    assert [lease["id"] for lease in doc(usage)["leases"] if lease["pid"] == gone] == []


@pytest.mark.usefixtures("spending_none")
def test_leases_of_live_runs_on_this_host_still_block(usage):
    here = socket.gethostname()
    write_leases(usage, (os.getpid(), here, 10.0), (os.getpid(), here, 20.0))  # other run ids, live pid
    with pytest.raises(BudgetExceeded, match="runs are already active"):
        file_budget().charge(GOOGLE_FREE)


@pytest.mark.usefixtures("spending_none")
def test_leases_from_another_host_follow_the_time_rule(usage):
    gone = dead_pid()  # the pid means nothing on another machine
    write_leases(usage, (gone, "some-other-host", 10.0), (gone, "some-other-host", 20.0))
    with pytest.raises(BudgetExceeded, match="runs are already active"):
        file_budget().charge(GOOGLE_FREE)
    window = 900.0 + budget_mod.CLIENT_TIMEOUT_S + budget_mod.LEASE_GRACE_S
    write_leases(usage, (gone, "some-other-host", window + 5), (gone, "some-other-host", window + 6))
    file_budget().charge(GOOGLE_FREE)


@pytest.mark.usefixtures("spending_none")
def test_when_the_pid_check_is_not_permitted_the_time_rule_applies(usage, monkeypatch):
    here = socket.gethostname()
    write_leases(usage, (dead_pid(), here, 10.0), (dead_pid(), here, 20.0))

    def forbidden(pid, sig):
        raise PermissionError

    monkeypatch.setattr(budget_mod.os, "kill", forbidden)
    with pytest.raises(BudgetExceeded, match="runs are already active"):
        file_budget().charge(GOOGLE_FREE)


@pytest.mark.usefixtures("spending_none")
def test_close_warns_on_stderr_when_the_lease_cannot_be_released(usage, caplog):
    b = Budget(Ledger(config.ledger_path(), lock_timeout=0.3))
    b.charge(GOOGLE_FREE)
    fd = os.open(f"{usage}.lock", os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)  # another process holds the store lock
    try:
        started = time.monotonic()
        with caplog.at_level(logging.WARNING, logger="maps.budget"):
            b.close()
        assert time.monotonic() - started < 5  # bounded by the lock deadline
    finally:
        os.close(fd)
    assert len(doc(usage)["leases"]) == 1
    assert "lease" in caplog.text and "could not be released" in caplog.text


@pytest.mark.usefixtures("spending_none")
def test_close_retries_while_the_lock_is_busy(usage, caplog):
    b = Budget(Ledger(config.ledger_path(), lock_timeout=5.0))
    b.charge(GOOGLE_FREE)
    fd = os.open(f"{usage}.lock", os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    threading.Timer(0.3, lambda: os.close(fd)).start()
    with caplog.at_level(logging.WARNING, logger="maps.budget"):
        b.close()
    assert doc(usage)["leases"] == [] and not caplog.records


# --- 5: "today" is the billing time zone's today -------------------------------------------------

from datetime import UTC, date, datetime  # noqa: E402


def freeze_billing_clock(monkeypatch, instant_utc):
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant_utc.astimezone(tz) if tz else instant_utc.replace(tzinfo=None)

    monkeypatch.setattr(budget_mod, "datetime", FrozenDatetime)


def test_price_freshness_and_initialised_dates_use_the_billing_time_zone(grant_spend, usage, monkeypatch):
    # 2027-01-01 03:00 UTC is still 2026-12-31 in Pacific time (the billing zone); the host date differs.
    freeze_billing_clock(monkeypatch, datetime(2027, 1, 1, 3, 0, tzinfo=UTC))
    assert budget_mod._today() == date(2026, 12, 31)
    grant_spend("google", "enabled", prices_checked_on="2026-12-31")
    assert doc(usage)["initialized"] == {"google": "2026-12-31"}
    file_budget().charge(GOOGLE_FREE)  # confirmed "today" in the billing zone: fresh, not in the future
    grant_spend("google", "enabled", prices_checked_on="2026-09-30")  # 92 days before 2026-12-31
    with pytest.raises(BudgetExceeded, match="prices not confirmed within 90 days"):
        file_budget().charge(GOOGLE_FREE)
    grant_spend("google", "enabled", prices_checked_on="2026-10-02")  # exactly 90 days
    file_budget().charge(GOOGLE_FREE)
