import pytest

from maps.models import Hit, Place
from maps.scoring import OBJECTIVES, bayesian_rating, rank, visibility


def hit(level=0, rank_=1, query="bakery"):
    return Hit("sim", query, level=level, rank=rank_)


def test_bayesian_rating_shrinks_small_samples_toward_the_prior():
    few = bayesian_rating(5.0, 2, prior_mean=4.2, prior_weight=20)
    many = bayesian_rating(4.8, 500, prior_mean=4.2, prior_weight=20)
    assert few < many
    assert 4.2 < few < 4.3
    assert many == pytest.approx(4.8, abs=0.03)


def test_bayesian_rating_of_an_unrated_place_is_the_prior():
    assert bayesian_rating(None, None, prior_mean=4.1) == 4.1


def test_visibility_reports_best_top_level_rank_and_first_level_seen():
    p = Place("p", hits=[hit(0, 14), hit(0, 3, "مخبز"), hit(2, 1)])
    v = visibility(p)
    assert v == {"n_hits": 3, "n_queries": 2, "best_top_rank": 3, "min_level": 0}


def test_place_seen_only_in_subdivided_cells_has_no_top_level_rank():
    v = visibility(Place("p", hits=[hit(2, 5), hit(3, 1)]))
    assert v["best_top_rank"] is None and v["min_level"] == 2


def corpus():
    return [
        Place("famous", name="Famous", rating=4.6, review_count=3000, hits=[hit(0, 1)]),
        Place("solid", name="Solid", rating=4.4, review_count=400, hits=[hit(0, 5)]),
        Place("gem", name="Gem", rating=4.9, review_count=45, hits=[hit(3, 2)]),
        Place("fluke", name="Fluke", rating=5.0, review_count=1, hits=[hit(3, 9)]),
        Place("weak", name="Weak", rating=3.6, review_count=800, hits=[hit(0, 9)]),
    ]


def order(objective):
    return [s.place.place_id for s in rank(corpus(), objective)]


def test_best_ranks_well_evidenced_quality_above_a_single_five_star_review():
    result = order("best")
    assert result.index("famous") < result.index("fluke")
    assert result[-1] == "weak"


def test_gems_puts_the_hidden_high_quality_place_first_and_demotes_the_famous_one():
    result = order("gems")
    assert result[0] == "gem"
    assert result.index("gem") < result.index("famous")


def test_gems_does_not_reward_one_review_wonders_over_the_real_gem():
    result = order("gems")
    assert result.index("gem") < result.index("fluke")


def test_hire_distrusts_thin_evidence_more_than_best_does():
    assert order("best").index("fluke") < order("best").index("solid")
    assert order("hire").index("fluke") > order("hire").index("solid")


def test_explore_interleaves_types_instead_of_listing_one_type_first():
    places = [
        Place(f"b{i}", rating=4.8 - i * 0.01, review_count=200, primary_type="bakery", hits=[hit()])
        for i in range(4)
    ] + [Place("bookshop", rating=4.5, review_count=200, primary_type="book_store", hits=[hit()])]
    result = [s.place.place_id for s in rank(places, "explore")]
    assert result.index("bookshop") <= 1


def test_top_limits_the_result_and_scores_explain_themselves():
    scored = rank(corpus(), "gems", top=2)
    assert len(scored) == 2
    assert {"bayes", "hidden_bonus", "popularity_penalty"} <= set(scored[0].parts)


def test_every_documented_objective_is_rankable():
    assert set(OBJECTIVES) == {"best", "gems", "explore", "find", "hire", "investigate"}
    for name in OBJECTIVES:
        assert len(rank(corpus(), name)) == 5


def test_unknown_objective_is_an_error():
    with pytest.raises(ValueError):
        rank(corpus(), "nope")


def test_an_unranked_sighting_does_not_count_as_visible_in_ordinary_search():
    enumerated = Place("p", hits=[Hit("google_aggregate", "type:bakery", level=0, rank=0)])
    assert visibility(enumerated)["best_top_rank"] is None


def unrated_corpus():
    return [
        Place("unrated", name="U", rating=None, review_count=None, hits=[hit(0, 1), hit(0, 2, "q2")]),
        Place("good", name="G", rating=4.6, review_count=12, hits=[hit(0, 3)]),
        Place("ok", name="O", rating=3.8, review_count=300, hits=[hit(0, 4)]),
    ]


@pytest.mark.parametrize("objective", ["best", "gems", "explore", "hire", "investigate"])
def test_unrated_places_rank_after_every_rated_place(objective):
    scored = rank(unrated_corpus(), objective)
    assert scored[-1].place.place_id == "unrated"
    assert scored[-1].parts["unrated"] == 1.0
    assert all("unrated" not in s.parts for s in scored[:-1])


def test_unrated_places_are_ordered_among_themselves_by_distinct_queries():
    places = unrated_corpus() + [
        Place("u2", rating=None, hits=[hit(0, 1)]),
        Place("u3", rating=None, hits=[hit(0, 1), hit(0, 1, "a"), hit(0, 1, "b")]),
    ]
    for objective in ("best", "hire", "explore"):
        ids = [s.place.place_id for s in rank(places, objective)]
        assert ids[2:] == ["u3", "unrated", "u2"], objective


def test_find_is_unchanged_by_missing_ratings():
    scored = rank(unrated_corpus(), "find")
    assert scored[0].place.place_id == "unrated"  # two distinct queries
    assert all("unrated" not in s.parts for s in scored)


def test_hire_treats_unknown_review_count_as_no_evidence():
    scored = {
        s.place.place_id: s for s in rank([Place("r", rating=4.5, review_count=None, hits=[hit()])], "hire")
    }
    assert scored["r"].parts["uncertainty"] == pytest.approx(0.5)
