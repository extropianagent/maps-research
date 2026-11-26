from maps.budget import Budget, Ledger
from maps.geography import BBox
from maps.models import Query
from maps.sim import SimProvider

AREA = BBox(24.5, 46.5, 24.9, 46.9)


def make(n=600, seed=7):
    return SimProvider(Budget(Ledger(":memory:")), area=AREA, n_places=n, seed=seed)


def test_same_seed_gives_the_same_world_and_other_seeds_differ():
    a, b, c = make(seed=1), make(seed=1), make(seed=2)
    assert [p.to_dict() for p in a.world] == [p.to_dict() for p in b.world]
    assert [p.to_dict() for p in a.world] != [p.to_dict() for p in c.world]
    assert a.world[0].place_id == "sim_000000"


def test_search_is_capped_at_60_and_flags_saturation():
    page = make().text_search(Query("bakery"), bbox=AREA)
    assert len(page.places) == 60 and page.saturated


def test_small_search_is_not_saturated():
    sim = make(n=40)
    page = sim.text_search(Query("bakery"), bbox=AREA)
    assert not page.saturated


def test_smaller_bbox_surfaces_places_the_whole_area_search_missed():
    sim = make()
    whole = {p.place_id for p in sim.text_search(Query("bakery"), bbox=AREA).places}
    extra = set()
    for cell in AREA.split():
        extra |= {p.place_id for p in sim.text_search(Query("bakery"), bbox=cell, level=1).places}
    assert extra - whole


def test_hits_are_tagged_with_provider_query_cell_level_and_rank():
    sim = make()
    cell = AREA.split()[0]
    page = sim.text_search(Query("bakery", "en"), bbox=cell, level=1)
    for i, p in enumerate(page.places, 1):
        (h,) = p.hits
        assert (h.provider, h.query, h.language, h.cell, h.level, h.rank) == (
            "sim", "bakery", "en", cell.key(), 1, i,
        )
        assert cell.contains(p.lat, p.lng)


def test_results_are_copies_of_the_world():
    sim = make()
    page = sim.text_search(Query("bakery"), bbox=AREA)
    page.places[0].name = "mutated"
    page.places[0].types.append("x")
    page.places[0].hits.clear()
    again = sim.text_search(Query("bakery"), bbox=AREA)
    assert "mutated" not in {p.name for p in sim.world}
    assert again.places[0].name != "mutated" and again.places[0].hits


def test_some_places_match_only_the_arabic_keyword():
    sim = make()
    ar, en = Query("مخبز"), Query("bakery")
    ar_only = [p for p in sim.world if sim.matches(p, ar) and not sim.matches(p, en)]
    assert ar_only


def test_reviews_are_deterministic_and_respect_limit():
    a, b = make(), make()
    pid = next(p.place_id for p in a.world if (p.review_count or 0) >= 10)
    ra, rb = a.reviews(pid, limit=4), b.reviews(pid, limit=4)
    assert len(ra) == 4
    assert [r.to_dict() for r in ra] == [r.to_dict() for r in rb]
    assert all(r.source == "sim" and r.place_id == pid for r in ra)


def test_a_manipulated_place_has_a_burst_of_short_five_star_one_review_authors():
    from datetime import datetime

    sim = make(n=1000)
    assert sim.manipulated_ids
    pid = sorted(sim.manipulated_ids)[0]
    revs = sim.reviews(pid, limit=30)
    burst = [
        r for r in revs
        if r.rating == 5 and len(r.text) < 15 and r.author_review_count == 1
    ]
    assert len(burst) >= 12
    times = sorted(datetime.fromisoformat(r.published) for r in burst)
    assert (times[-1] - times[0]).days <= 5


def test_budget_is_charged_per_search_and_per_reviews_call():
    sim = make()
    sim.text_search(Query("bakery"), bbox=AREA)
    sim.text_search(Query("cafe"), bbox=None)
    assert sim.budget.run_units["sim.search"] == 2
    sim.reviews(sim.world[0].place_id)
    assert sim.budget.run_units["sim.reviews"] == 1
