import pytest

from maps.budget import Budget, BudgetExceeded, Ledger
from maps.geography import BBox
from maps.models import Query
from maps.providers.base import SearchPage
from maps.retrieval import DEPTHS, Depth, discover
from maps.sim import SimProvider
from maps.store import RunStore

AREA = BBox(24.5, 46.5, 24.9, 46.9)


def budget():
    return Budget(Ledger(":memory:"))


def sim(n=600, seed=7):
    return SimProvider(budget(), area=AREA, n_places=n, seed=seed)


def truth(provider, query="bakery"):
    return {p.place_id for p in provider.world if provider.matches(p, Query(query))}


def recall(provider, depth_name, query="bakery"):
    found = discover([provider], [Query(query)], AREA, DEPTHS[depth_name]).places
    return len(set(found) & truth(provider, query)) / len(truth(provider, query))


def test_the_simulated_city_has_more_matches_than_one_search_can_return():
    assert len(truth(sim())) > 60 * 2


def test_a_single_search_is_capped_and_reports_saturation():
    page = sim().text_search(Query("bakery"), bbox=AREA, max_results=60)
    assert len(page.places) == 60 and page.saturated


def test_quick_depth_does_not_subdivide():
    p = sim()
    result = discover([p], [Query("bakery")], AREA, DEPTHS["quick"])
    assert result.tasks_run == 1
    assert len(result.places) == DEPTHS["quick"].max_results


def test_deeper_search_finds_strictly_more_of_the_city():
    quick, standard, deep = (recall(sim(), d) for d in ("quick", "standard", "deep"))
    assert quick < standard < deep


def test_exhaustive_search_recovers_nearly_everything():
    assert recall(sim(), "exhaustive") >= 0.98


def test_places_are_deduplicated_by_id_and_keep_every_sighting():
    p = sim()
    result = discover([p], [Query("bakery"), Query("مخبز", "ar")], AREA, DEPTHS["standard"])
    multi = [pl for pl in result.places.values() if len({h.query for h in pl.hits}) == 2]
    assert multi, "a place matching both keywords should carry hits from both"
    assert len(result.places) == len({pl.place_id for pl in result.places.values()})


def test_unsaturated_cells_are_not_subdivided():
    p = sim(n=40)
    result = discover([p], [Query("bakery")], AREA, DEPTHS["exhaustive"])
    assert result.tasks_run == 1 and not result.unresolved


def test_cells_still_saturated_at_the_depth_limit_are_reported_as_gaps():
    p = sim()
    result = discover([p], [Query("bakery")], AREA, Depth("shallow", max_level=1, max_results=60, max_tasks=100))
    assert result.unresolved, "saturated leaves must be reported, not silently dropped"


def test_max_tasks_stops_the_search_and_says_why():
    p = sim()
    result = discover([p], [Query("bakery")], AREA, Depth("tiny", max_level=6, max_results=60, max_tasks=3))
    assert result.tasks_run == 3
    assert "max_tasks" in result.stopped


class StingyProvider(SimProvider):
    """Raises BudgetExceeded on the Nth search, like a provider hitting its free cap."""

    def __init__(self, *a, allow, **kw):
        super().__init__(*a, **kw)
        self.allow = allow

    def text_search(self, query, **kw) -> SearchPage:
        if self.allow <= 0:
            raise BudgetExceeded("free allowance exhausted")
        self.allow -= 1
        return super().text_search(query, **kw)


def test_budget_exhaustion_keeps_partial_results_and_reports_the_reason():
    p = StingyProvider(budget(), area=AREA, n_places=600, seed=7, allow=2)
    result = discover([p], [Query("bakery")], AREA, DEPTHS["deep"])
    assert result.tasks_run == 2
    assert result.places
    assert "free allowance exhausted" in result.stopped


def test_resuming_a_run_does_not_repeat_completed_searches(tmp_path):
    store = RunStore.create(tmp_path, {})
    first = StingyProvider(budget(), area=AREA, n_places=600, seed=7, allow=3)
    partial = discover([first], [Query("bakery")], AREA, DEPTHS["deep"], store=store)
    assert partial.stopped

    second = sim()
    calls = []
    original = second.text_search
    second.text_search = lambda q, **kw: (calls.append(kw["bbox"].key()), original(q, **kw))[1]
    resumed = discover([second], [Query("bakery")], AREA, DEPTHS["deep"], store=RunStore.open(tmp_path, store.run_id))

    assert resumed.tasks_resumed == 3
    assert AREA.key() not in calls, "the top-level search was already done"
    fresh = discover([sim()], [Query("bakery")], AREA, DEPTHS["deep"])
    assert set(resumed.places) == set(fresh.places)


def test_min_cell_size_stops_subdivision_of_tiny_cells():
    p = sim()
    coarse = Depth("coarse", max_level=10, max_results=60, max_tasks=10_000, min_cell_m=30_000)
    result = discover([p], [Query("bakery")], AREA, coarse)
    assert max(h.level for pl in result.places.values() for h in pl.hits) <= 1


def test_depth_presets_are_ordered_from_cheap_to_thorough():
    names = ["quick", "standard", "deep", "exhaustive"]
    assert list(DEPTHS) == names
    levels = [DEPTHS[n].max_level for n in names]
    assert levels == sorted(levels) and levels[0] == 0


def test_a_saturated_quick_search_is_not_reported_as_complete():
    result = discover([sim()], [Query("bakery")], AREA, DEPTHS["quick"])
    assert not result.complete
    assert result.unresolved[0]["reason"].startswith("saturated")
