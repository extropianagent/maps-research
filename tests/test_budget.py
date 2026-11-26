import pytest

from maps import budget as budget_mod
from maps.budget import Budget, BudgetExceeded, Ledger
from maps.pricing import SKUS

# The owner's spending authorization (billing enabled, generous caps) is recorded for every subject, so
# these tests exercise the cost rules themselves. The gateway rules are in test_gateway.py.
pytestmark = pytest.mark.usefixtures("spending_enabled")


@pytest.fixture
def ledger():
    return Ledger(":memory:")


def test_free_mode_allows_requests_inside_the_free_allowance(ledger):
    b = Budget(ledger)
    q = b.charge("google.text_search.enterprise")
    assert q.out_of_pocket_usd == 0
    assert ledger.units("google.text_search.enterprise") == 1


def test_free_mode_stops_at_the_reserve_before_the_allowance_runs_out(ledger):
    free = SKUS["google.text_search.enterprise"].free_units
    ledger.add("google.text_search.enterprise", free * 0.9, 0)
    with pytest.raises(BudgetExceeded, match="90%"):
        Budget(ledger).charge("google.text_search.enterprise")


def test_refused_charge_is_not_recorded(ledger):
    free = SKUS["google.text_search.enterprise"].free_units
    ledger.add("google.text_search.enterprise", free, 0)
    with pytest.raises(BudgetExceeded):
        Budget(ledger).charge("google.text_search.enterprise")
    assert ledger.units("google.text_search.enterprise") == free


def test_free_mode_refuses_a_sku_with_no_free_allowance(ledger):
    with pytest.raises(BudgetExceeded, match="--cost quality"):
        Budget(ledger).charge("dataforseo.maps_search")


def test_unlimited_free_sku_is_never_refused(ledger):
    b = Budget(ledger)
    b.charge("google.text_search.ids", 1_000_000)
    assert b.run_usd == 0


def test_provider_credit_makes_paid_units_free_until_it_is_used_up(ledger):
    b = Budget(ledger)
    q = b.charge("apify.place", 100)
    assert q.billable_usd == pytest.approx(0.4)
    assert q.out_of_pocket_usd == 0
    with pytest.raises(BudgetExceeded, match="credit"):
        b.charge("apify.place", 1100)  # would pass 90% of the $5 monthly credit


def test_quality_mode_spends_up_to_max_usd_and_then_stops(ledger):
    b = Budget(ledger, mode="quality", max_usd=0.01)
    for _ in range(5):
        b.charge("dataforseo.maps_search")
    assert b.run_usd == pytest.approx(0.01)
    with pytest.raises(BudgetExceeded, match="authorized"):
        b.charge("dataforseo.maps_search")


def test_quality_mode_with_zero_budget_still_uses_free_allowances(ledger):
    b = Budget(ledger, mode="quality", max_usd=0)
    b.charge("google.text_search.pro")
    with pytest.raises(BudgetExceeded):
        b.charge("dataforseo.maps_search")


def test_max_usd_is_rejected_in_free_mode(ledger):
    with pytest.raises(ValueError):
        Budget(ledger, mode="free", max_usd=3)


def test_usage_is_counted_per_billing_month(ledger, monkeypatch):
    monkeypatch.setattr(budget_mod, "current_month", lambda: "2026-11")
    Budget(ledger).charge("google.text_search.pro", 10)
    monkeypatch.setattr(budget_mod, "current_month", lambda: "2026-12")
    assert ledger.units("google.text_search.pro") == 0


def test_ledger_persists_across_processes(tmp_path):
    path = tmp_path / "state" / "usage.json"
    Budget(Ledger(path)).charge("google.aggregate", 3)
    assert Ledger(path).units("google.aggregate") == 3


def test_month_report_lists_every_sku_with_remaining_free_units(ledger):
    Budget(ledger).charge("google.text_search.pro", 10)
    row = next(r for r in Budget(ledger).month_report() if r["sku"] == "google.text_search.pro")
    assert row["used"] == 10
    assert row["remaining_free"] == SKUS["google.text_search.pro"].free_units - 10


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1.0])
def test_max_usd_must_be_finite_and_non_negative(ledger, bad):
    with pytest.raises(ValueError):
        Budget(ledger, mode="quality", max_usd=bad)


@pytest.mark.parametrize("bad", [0, 0.0, -0.1, 1.5, float("nan")])
def test_reserve_must_be_in_unit_interval(ledger, bad):
    with pytest.raises(ValueError):
        Budget(ledger, reserve=bad)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1])
@pytest.mark.parametrize("method", ["charge", "check", "quote"])
def test_units_must_be_finite_and_non_negative(ledger, method, bad):
    with pytest.raises(ValueError):
        getattr(Budget(ledger), method)("google.text_search.enterprise", bad)
    assert ledger.units("google.text_search.enterprise") == 0
