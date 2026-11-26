from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from maps.models import Place, Review
from maps.reviews import (
    ReviewIndex,
    co_review_graph,
    contributor_overlap,
    integrity_report,
    tokenize,
)


def rv(pid: str, text: str = "", rating: float | None = None, **kw) -> Review:
    return Review(place_id=pid, text=text, rating=rating, **kw)


def place(pid: str, reviews: list[Review], **kw) -> Place:
    return Place(place_id=pid, name=kw.pop("name", pid.upper()), reviews=reviews, **kw)


# --- tokenize ----------------------------------------------------------------------------------


def test_tokenize_english_lowercases_and_strips_punctuation():
    assert tokenize("Great COFFEE, friendly-staff! a 5") == ["great", "coffee", "friendly", "staff"]


def test_tokenize_arabic_normalisation():
    assert tokenize("مَكْتَبَة") == ["مكتبه"]
    assert tokenize("أحمد إبراهيم آمال") == ["احمد", "ابراهيم", "امال"]
    assert tokenize("مستشفى") == ["مستشفي"]
    assert tokenize("جمـــيل") == ["جميل"]
    assert tokenize("القهوة، رائعة!") == ["قهوه", "رائعه"]


def test_tokenize_variants_collide():
    assert tokenize("المكتبة") == tokenize("المَكتبه")


def test_tokenize_empty():
    assert tokenize("") == []
    assert tokenize("! ? .") == []


# --- BM25 --------------------------------------------------------------------------------------


@pytest.fixture
def corpus() -> list[Place]:
    return [
        place(
            "a",
            [
                rv("a", "great coffee and cozy seats", 5),
                rv("a", "coffee was cold", 2),
                rv("a", "excellent pistachio croissant and coffee", 5),
            ],
        ),
        place("b", [rv("b", "nice view, coffee fine", 4), rv("b", "", 5)]),
        place("c", [rv("c", "قهوة ممتازة وخدمة سريعة", 5), rv("c", "the parking is terrible", 1)]),
    ]


def test_index_skips_textless_reviews(corpus):
    assert len(ReviewIndex(corpus)) == 6


def test_search_term_presence_outranks_absence(corpus):
    res = ReviewIndex(corpus).search("parking")
    assert len(res) == 1 and res[0].place_id == "c"
    assert all(m.score > 0 for m in ReviewIndex(corpus).search("coffee"))


def test_rarer_term_weighs_more(corpus):
    res = ReviewIndex(corpus).search("coffee pistachio")
    assert "pistachio" in res[0].review.text
    idx = ReviewIndex(corpus)
    assert idx.search("pistachio")[0].score > idx.search("coffee")[0].score


def test_search_arabic_matches_across_spelling(corpus):
    res = ReviewIndex(corpus).search("قهوه")
    assert [m.place_id for m in res] == ["c"]
    assert res[0].place_name == "C"


def test_search_sorted_and_top(corpus):
    res = ReviewIndex(corpus).search("coffee", top=2)
    assert len(res) == 2
    assert res[0].score >= res[1].score


def test_rating_filters(corpus):
    idx = ReviewIndex(corpus)
    assert {m.review.rating for m in idx.search("coffee", min_rating=4)} == {4, 5}
    assert [m.review.rating for m in idx.search("coffee", max_rating=2)] == [2]
    assert len(idx.search("coffee", min_rating=3, max_rating=4)) == 1


def test_no_match_and_empty_index():
    assert ReviewIndex([]).search("x yz") == []
    assert ReviewIndex([place("a", [rv("a", "hello world")])]).search("absent") == []


def test_search_places_aggregates(corpus):
    res = ReviewIndex(corpus).search_places("coffee")
    assert [p.place_id for p in res] == ["a", "b"]
    a = res[0]
    assert a.n_matches == 3
    assert a.best.score == max(m.score for m in ReviewIndex(corpus).search("coffee") if m.place_id == "a")
    a_scores = [m.score for m in ReviewIndex(corpus).search("coffee") if m.place_id == "a"]
    per_review = sorted(a_scores, reverse=True)
    assert a.score == pytest.approx(sum(per_review[:3]))


def test_search_places_caps_at_top_three():
    reviews = [rv("p", f"tea tea review number{i}") for i in range(5)]
    idx = ReviewIndex([place("p", reviews), place("q", [rv("q", "something else")])])
    pm = idx.search_places("tea")[0]
    assert pm.n_matches == 5
    assert pm.score == pytest.approx(sum(m.score for m in idx.search("tea")[:3]))


# --- integrity ---------------------------------------------------------------------------------


def test_confidence_tiers():
    def conf(n: int) -> str:
        return integrity_report(place("p", [rv("p", "x", 4) for _ in range(n)]))["confidence"]

    assert [conf(0), conf(4), conf(5), conf(19), conf(20), conf(99), conf(100)] == [
        "none",
        "none",
        "low",
        "low",
        "medium",
        "medium",
        "high",
    ]


def test_signals_degrade_to_none_without_data():
    rep = integrity_report(place("p", [rv("p", "", None), rv("p", "", None)]))
    s = rep["signals"]
    for key in (
        "five_star_share",
        "one_star_share",
        "polarization",
        "textless_five_star_share",
        "duplicate_text_share",
        "burst_share",
        "single_review_author_share",
        "rating_gap",
    ):
        assert s[key] is None, key
    assert rep["flags"] == []
    assert rep["place_id"] == "p" and rep["n_reviews"] == 2


def test_empty_place_report():
    rep = integrity_report(place("p", []))
    assert rep["n_reviews"] == 0 and rep["confidence"] == "none"
    assert all(v is None for v in rep["signals"].values())


def test_rating_shares_and_polarization_flag():
    reviews = [rv("p", "good", 5) for _ in range(6)] + [rv("p", "bad", 1) for _ in range(4)]
    rep = integrity_report(place("p", reviews))
    s = rep["signals"]
    assert s["five_star_share"] == 0.6 and s["one_star_share"] == 0.4 and s["polarization"] == 1.0
    assert any("polarised" in f for f in rep["flags"])


def test_five_star_share_flag_and_textless():
    reviews = [rv("p", "", 5) for _ in range(8)]
    reviews += [rv("p", "really lovely place to visit", 5) for _ in range(2)]
    rep = integrity_report(place("p", reviews))
    assert rep["signals"]["five_star_share"] == 1.0
    assert rep["signals"]["textless_five_star_share"] == 0.8
    assert any("5★" in f and "sampled" in f for f in rep["flags"])
    assert any("little or no text" in f for f in rep["flags"])


def test_textless_none_without_five_star():
    rep = integrity_report(place("p", [rv("p", "", 3)] * 5))
    assert rep["signals"]["textless_five_star_share"] is None


def test_no_flags_below_minimum_sample():
    rep = integrity_report(place("p", [rv("p", "", 5)] * 4))
    assert rep["signals"]["five_star_share"] == 1.0
    assert rep["flags"] == []


def test_duplicate_text_share():
    dup = "Best restaurant in town, amazing food and service!"
    reviews = [
        rv("p", dup, 5),
        rv("p", "best restaurant in town amazing food -- and service", 5),
        rv("p", "ok", 3),
        rv("p", "ok", 3),
        rv("p", "a different and sufficiently long review text here", 4),
    ]
    rep = integrity_report(place("p", reviews))
    assert rep["signals"]["duplicate_text_share"] == 0.4
    assert any("repeated" in f for f in rep["flags"])


def _ts(base: datetime, days: float) -> str:
    return (base + timedelta(days=days)).isoformat()


def test_burst_detection():
    base = datetime(2025, 1, 1, tzinfo=UTC)
    days = [0, 1, 2, 3, 4, 5, 100, 200, 300, 400]
    reviews = [rv("p", "fine", 4, published=_ts(base, d)) for d in days]
    rep = integrity_report(place("p", reviews))
    assert rep["signals"]["burst_share"] == 0.6
    assert rep["signals"]["dated_span_days"] == 400
    assert any("7-day window" in f for f in rep["flags"])


def test_burst_not_flagged_when_spread_or_young():
    base = datetime(2025, 1, 1, tzinfo=UTC)
    spread = [rv("p", "fine", 4, published=_ts(base, 30 * i)) for i in range(10)]
    rep = integrity_report(place("p", spread))
    assert rep["signals"]["burst_share"] == 0.1
    assert not any("7-day" in f for f in rep["flags"])
    young = [rv("p", "fine", 4, published=_ts(base, i / 2)) for i in range(10)]
    rep = integrity_report(place("p", young))
    assert rep["signals"]["burst_share"] == 1.0
    assert not any("7-day" in f for f in rep["flags"])


def test_burst_needs_enough_dated_reviews_and_handles_z_and_garbage():
    reviews = [rv("p", "x", 4, published=f"2025-03-0{i + 1}T00:00:00Z") for i in range(7)]
    reviews.append(rv("p", "x", 4, published="not a date"))
    assert integrity_report(place("p", reviews))["signals"]["burst_share"] is None
    reviews.append(rv("p", "x", 4, published="2025-03-09"))
    assert integrity_report(place("p", reviews))["signals"]["burst_share"] == pytest.approx(7 / 8, abs=1e-4)


def test_single_review_author_share():
    reviews = [rv("p", "x", 5, author_review_count=1) for _ in range(3)]
    reviews += [rv("p", "x", 4, author_review_count=40), rv("p", "x", 4, author_review_count=9)]
    reviews.append(rv("p", "x", 4))
    rep = integrity_report(place("p", reviews))
    assert rep["signals"]["single_review_author_share"] == 0.6
    assert any("only this one review" in f for f in rep["flags"])


def test_rating_gap_is_a_caveat_flag():
    reviews = [rv("p", "x", 2) for _ in range(5)]
    rep = integrity_report(place("p", reviews, rating=4.5))
    assert rep["signals"]["rating_gap"] == 2.5
    assert any("unrepresentative" in f for f in rep["flags"])
    assert integrity_report(place("p", reviews))["signals"]["rating_gap"] is None


def test_owner_reply_share():
    reviews = [rv("p", "x", 4, owner_reply="thanks!"), rv("p", "x", 4, owner_reply="  "), rv("p", "x", 4)]
    share = integrity_report(place("p", reviews))["signals"]["owner_reply_share"]
    assert share == pytest.approx(1 / 3, abs=1e-4)


def test_caveat_mentions_partial_sample():
    rep = integrity_report(place("p", [rv("p", "x", 4)] * 3, review_count=250))
    assert "3 of 250" in rep["caveat"] and "not a verdict" in rep["caveat"]
    rep = integrity_report(place("p", [rv("p", "x", 4)] * 3, review_count=3))
    assert "Partial" not in rep["caveat"] and "not a verdict" in rep["caveat"]


def test_healthy_place_has_no_flags():
    base = datetime(2024, 1, 1, tzinfo=UTC)
    ratings = [5, 4, 4, 3, 5, 4, 2, 4, 5, 3]
    words = "one two three four five six seven eight nine ten".split()
    reviews = [
        rv(
            "p",
            f"visit {words[i]} was {'good' if r > 3 else 'mixed'} overall",
            r,
            published=_ts(base, 40 * i),
            author_review_count=10 + i,
        )
        for i, r in enumerate(ratings)
    ]
    rep = integrity_report(place("p", reviews, rating=3.9, review_count=10))
    assert rep["flags"] == []
    assert not any(k.startswith("is_") for k in rep["signals"])


# --- contributor graph -------------------------------------------------------------------------


@pytest.fixture
def social() -> list[Place]:
    return [
        place(
            "liked",
            [
                rv("liked", "x", 5, author="Ann", author_id="u1"),
                rv("liked", "x", 5, author="Bob", author_id="u2"),
                rv("liked", "x", 2, author="Cat", author_id="u3"),
            ],
        ),
        place(
            "p1",
            [
                rv("p1", "x", 5, author_id="u1"),
                rv("p1", "x", 4, author="Bob", author_id="u2"),
                rv("p1", "x", 5, author="Cat", author_id="u3"),
            ],
        ),
        place(
            "p2",
            [
                rv("p2", "x", 4, author="Ann", author_id="u1"),
                rv("p2", "x", 3, author_id="u2"),
                rv("p2", "x", 5, author="Anon"),
            ],
        ),
        place("p3", [rv("p3", "x", 5, author="Cat", author_id="u3")]),
    ]


def test_co_review_graph(social):
    g = co_review_graph(social)
    assert [d["author_id"] for d in g] == ["u1", "u2", "u3"]
    u1 = g[0]
    assert u1["author"] == "Ann" and u1["n_places"] == 3
    assert u1["mean_rating"] == pytest.approx(14 / 3, abs=1e-3)
    assert u1["places"][0] == {"place_id": "liked", "name": "LIKED", "rating": 5}
    assert all(d["n_places"] >= 2 for d in g)
    assert [d["author_id"] for d in co_review_graph(social, min_shared=4)] == []


def test_co_review_graph_orders_by_n_places():
    places = [place(f"p{i}", [rv(f"p{i}", "x", 4, author_id="z")]) for i in range(3)]
    places += [place("q0", [rv("q0", "x", 4, author_id="a")]), place("q1", [rv("q1", "x", 4, author_id="a")])]
    assert [d["author_id"] for d in co_review_graph(places)] == ["z", "a"]


def test_contributor_overlap(social):
    res = contributor_overlap(social, {"liked"})
    assert res == [
        {"place_id": "p1", "name": "P1", "supporters": 2, "via": ["u1", "u2"]},
        {"place_id": "p2", "name": "P2", "supporters": 1, "via": ["u1"]},
    ]
    strict = contributor_overlap(social, {"liked"}, min_rating=5)
    assert strict == [{"place_id": "p1", "name": "P1", "supporters": 1, "via": ["u1"]}]
    assert contributor_overlap(social, set()) == []


def test_flags_are_gated_by_each_signals_own_denominator():
    reviews = [
        rv("p", "", 5, author_review_count=1),
        rv("p", "a decent place, would come again", 3),
        rv("p", "so-so coffee and slow service today", 3),
        rv("p", "fine for a quick stop on the way", 4),
        rv("p", "average", None),
    ]
    rep = integrity_report(place("p", reviews))
    assert rep["n_reviews"] == 5
    assert rep["signals"]["textless_five_star_share"] == 1.0  # still reported
    assert rep["signals"]["single_review_author_share"] == 1.0
    assert rep["flags"] == []


def test_star_share_flags_need_enough_rated_reviews():
    reviews = [rv("p", "great", 5)] * 4 + [rv("p", "great")] * 4
    rep = integrity_report(place("p", reviews))
    assert rep["signals"]["five_star_share"] == 1.0 and rep["flags"] == []


def test_tokenize_strips_the_arabic_definite_article():
    assert tokenize("الخبز") == ["خبز"]
    assert tokenize("والخبز بالخبز كالخبز فالخبز للخبز") == ["خبز"] * 5
    assert tokenize("الى") == ["الي"]  # fewer than 3 characters would remain
    assert tokenize("لله") == ["لله"]


def test_arabic_search_matches_with_and_without_the_article():
    idx = ReviewIndex([place("p", [rv("p", "الخبز طازج جدا", 5)]), place("q", [rv("q", "قهوة عادية", 3)])])
    assert [m.place_id for m in idx.search("خبز")] == ["p"]
    assert [m.place_id for m in idx.search("الخبز")] == ["p"]


# --- partial matching --------------------------------------------------------------------------


def _one(*texts: str) -> ReviewIndex:
    return ReviewIndex([place("p", [rv("p", t) for t in texts])])


def test_query_term_partially_matches_an_inflected_form():
    res = _one("the cup cracked in transit", "lovely service").search("crack")
    assert len(res) == 1 and "cracked" in res[0].review.text


def test_arabic_query_matches_a_prefixed_form():
    res = _one("ذهبت لإصلاحه امس", "خدمة ممتازة").search("إصلاح")
    assert len(res) == 1 and "لإصلاحه" in res[0].review.text


def test_exact_match_outranks_partial_for_otherwise_identical_reviews():
    res = _one("the glass is cracked today", "the glass is crack today", "plain filler review").search(
        "crack"
    )
    assert [m.review.text for m in res] == ["the glass is crack today", "the glass is cracked today"]
    assert res[0].score > res[1].score > 0


def test_short_terms_match_exactly_only():
    assert _one("a big cabin here", "other words").search("ab") == []
    assert len(_one("ab cd here", "other words").search("ab")) == 1


def test_review_with_neither_form_scores_zero():
    assert _one("cracked", "nothing relevant").search("scratch") == []
    assert [m.review.text for m in _one("cracked", "nothing relevant").search("crack")] == ["cracked"]
