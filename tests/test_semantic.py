from __future__ import annotations

import json
import math
import re
import sys
import types

import pytest

from maps.models import Place, Review
from maps.reviews import ReviewIndex, ReviewMatch
from maps.semantic import (
    DEFAULT_MODEL,
    FastEmbedEmbedder,
    SemanticIndex,
    SemanticUnavailable,
    aggregate_by_place,
    cosine,
    hybrid_search,
)

# --- deterministic fake embedder ---------------------------------------------------------------

# Each concept is one dimension; any phrase in a concept's synonym list switches that dimension on.
CONCEPTS: list[list[str]] = [
    ["grout", "joints", "lines between the tiles", "فواصل", "crumbled"],
    ["late", "delayed", "تأخر", "waited an hour"],
    ["coffee", "espresso", "قهوة"],
    ["parking", "car park"],
]


def _vec(text: str) -> list[float]:
    low = text.casefold()
    return [float(sum(low.count(s) for s in syns)) for syns in CONCEPTS]


class FakeEmbedder:
    def __init__(self, name: str = "fake-v1") -> None:
        self.name = name
        self.doc_calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.doc_calls.append(list(texts))
        return [_vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return _vec(text)

    @property
    def n_embedded(self) -> int:
        return sum(len(c) for c in self.doc_calls)


def rv(pid: str, text: str = "", rating: float | None = None, **kw) -> Review:
    return Review(place_id=pid, text=text, rating=rating, **kw)


def place(pid: str, reviews: list[Review]) -> Place:
    return Place(place_id=pid, name=pid.upper(), reviews=reviews)


@pytest.fixture
def corpus() -> list[Place]:
    return [
        place(
            "a",
            [
                rv("a", "the lines between the tiles crumbled after a month", 2),
                rv("a", "great espresso and friendly staff", 5),
                rv("a", "the joints were cracked and the grout stained", 1),
            ],
        ),
        place(
            "b",
            [
                rv("b", "فواصل البلاط متهالكة", 3),
                rv("b", "service was delayed and we waited an hour", 2),
                rv("b", "", 5),
                rv("b", "   ", 4),
                rv("b", "نظيف جدا", None),  # no concept, no rating
            ],
        ),
        place("c", [rv("c", "no parking nearby", 4), rv("c", "قهوة جيدة", 5)]),
    ]


# --- cosine ------------------------------------------------------------------------------------


def test_cosine_basics():
    assert cosine([1, 0], [1, 0]) == pytest.approx(1.0)
    assert cosine([1, 0], [0, 1]) == pytest.approx(0.0)
    assert cosine([1, 0], [-1, 0]) == pytest.approx(-1.0)
    assert cosine([1, 1], [2, 2]) == pytest.approx(1.0)
    assert cosine([3, 4], [4, 3]) == pytest.approx(24 / 25)


def test_cosine_zero_vector_is_zero():
    assert cosine([0, 0], [1, 2]) == 0.0
    assert cosine([1, 2], [0, 0]) == 0.0
    assert cosine([0, 0], [0, 0]) == 0.0
    assert not math.isnan(cosine([], []))


# --- semantic search ---------------------------------------------------------------------------


def test_semantic_finds_paraphrase_and_arabic_where_keywords_find_nothing(corpus):
    query = "grout"
    assert not any(
        "lines between" in m.review.text or "فواصل" in m.review.text
        for m in ReviewIndex(corpus).search(query)
    )
    idx = SemanticIndex(corpus, FakeEmbedder())
    texts = [m.review.text for m in idx.search(query, min_score=0.1)]
    assert "the lines between the tiles crumbled after a month" in texts
    assert "فواصل البلاط متهالكة" in texts
    assert "great espresso and friendly staff" not in texts


def test_keyword_index_really_misses_the_paraphrase_and_arabic(corpus):
    # A query with no literal overlap: BM25 returns nothing at all.
    assert ReviewIndex(corpus).search("seams") == []
    assert ReviewIndex(corpus).search("grout joints")  # sanity: literal words do match something
    only_para = [p for p in corpus if p.place_id == "b"]
    assert ReviewIndex(only_para).search("grout") == []
    hits = SemanticIndex(only_para, FakeEmbedder()).search("grout", min_score=0.1)
    assert [m.review.text for m in hits] == ["فواصل البلاط متهالكة"]


def test_semantic_results_sorted_top_and_match_type(corpus):
    res = SemanticIndex(corpus, FakeEmbedder()).search("grout", top=2)
    assert len(res) == 2
    assert all(isinstance(m, ReviewMatch) for m in res)
    assert res[0].score >= res[1].score
    assert res[0].place_name == res[0].place_id.upper()


def test_rating_filters(corpus):
    idx = SemanticIndex(corpus, FakeEmbedder())
    q = "grout"
    low = idx.search(q, max_rating=2, min_score=0.1)
    assert {m.review.rating for m in low} == {1, 2}
    high = idx.search(q, min_rating=3, min_score=0.1)
    assert [m.review.rating for m in high] == [3]
    assert idx.search(q, min_rating=2, max_rating=2, min_score=0.1)[0].review.rating == 2


def test_unrated_review_drops_out_when_a_rating_filter_is_set(corpus):
    idx = SemanticIndex(corpus, FakeEmbedder())
    # "نظيف جدا" has no rating and zero similarity; use a query whose vector is all zero so it ties.
    texts_any = [m.review.text for m in idx.search("zzz", top=50)]
    assert "نظيف جدا" in texts_any
    assert "نظيف جدا" not in [m.review.text for m in idx.search("zzz", top=50, min_rating=1)]
    assert "نظيف جدا" not in [m.review.text for m in idx.search("zzz", top=50, max_rating=5)]


def test_min_score_filters_weak_matches(corpus):
    idx = SemanticIndex(corpus, FakeEmbedder())
    assert idx.search("grout", min_score=0.99)  # exact-concept reviews score 1.0
    assert all(m.score >= 0.99 for m in idx.search("grout", min_score=0.99))
    assert idx.search("grout", min_score=1.01) == []
    assert len(idx.search("grout", min_score=0.0)) >= len(idx.search("grout", min_score=0.5))


def test_empty_text_reviews_are_skipped(corpus):
    emb = FakeEmbedder()
    idx = SemanticIndex(corpus, emb)
    assert len(idx) == 8
    assert emb.n_embedded == 8
    assert all(m.review.text.strip() for m in idx.search("zzz", top=100))


def test_empty_index_search():
    assert SemanticIndex([], FakeEmbedder()).search("grout") == []


# --- hybrid fusion -----------------------------------------------------------------------------


@pytest.fixture
def hybrid_corpus() -> list[Place]:
    return [
        place(
            "h",
            [
                rv("h", "grout grout everywhere, the grout is awful", 2),  # both methods
                rv("h", "the lines between the tiles crumbled", 2),  # semantic only
                rv("h", "grout", 3),  # lexical hit, also semantic hit
                rv("h", "nice espresso", 5),
            ],
        ),
        place("k", [rv("k", "groutless but grouty thing", 3), rv("k", "paraphrase: joints failing", 3)]),
    ]


def test_hybrid_ranks_review_found_by_both_above_single_method_ones(hybrid_corpus):
    kw = ReviewIndex(hybrid_corpus)
    sem = SemanticIndex(hybrid_corpus, FakeEmbedder())
    both_text = "grout grout everywhere, the grout is awful"
    sem_only = "the lines between the tiles crumbled"
    kw_hit_texts = {m.review.text for m in kw.search("grout", top=50)}
    assert sem_only not in kw_hit_texts and both_text in kw_hit_texts

    res = hybrid_search(kw, sem, "grout")
    texts = [m.review.text for m in res]
    assert texts.index(both_text) < texts.index(sem_only)
    # Reviews found by only one method still appear.
    assert sem_only in texts
    assert "groutless but grouty thing" in texts  # lexical (partial) only
    assert [m.score for m in res] == sorted((m.score for m in res), reverse=True)


def test_hybrid_score_is_reciprocal_rank_fusion():
    places = [place("p", [rv("p", "grout is bad", 2), rv("p", "unrelated espresso", 5)])]
    kw = ReviewIndex(places)
    sem = SemanticIndex(places, FakeEmbedder())
    res = hybrid_search(kw, sem, "grout", k=60)
    assert res[0].review.text == "grout is bad"
    # Rank 1 in both lists: 2 / (60 + 1).
    assert res[0].score == pytest.approx(2 / 61)


def test_hybrid_rating_filter_and_top(hybrid_corpus):
    kw = ReviewIndex(hybrid_corpus)
    sem = SemanticIndex(hybrid_corpus, FakeEmbedder())
    res = hybrid_search(kw, sem, "grout", min_rating=3)
    assert res and all(m.review.rating is not None and m.review.rating >= 3 for m in res)
    assert len(hybrid_search(kw, sem, "grout", top=1)) == 1


def test_hybrid_does_not_double_count_equal_reviews_from_separate_objects():
    # Two equal-looking reviews from different places must stay distinct; same review must fuse.
    places = [
        place("p", [rv("p", "grout", 3, author="Ann")]),
        place("q", [rv("q", "grout", 3, author="Ann")]),
    ]
    res = hybrid_search(ReviewIndex(places), SemanticIndex(places, FakeEmbedder()), "grout")
    assert sorted(m.place_id for m in res) == ["p", "q"]


# --- aggregate_by_place ------------------------------------------------------------------------


def test_aggregate_by_place_mirrors_search_places():
    reviews = [rv("p", f"tea tea review number{i}") for i in range(5)]
    places = [place("p", reviews), place("q", [rv("q", "something else")])]
    idx = ReviewIndex(places)
    expected = idx.search_places("tea")[0]
    got = aggregate_by_place(idx.search("tea", top=100))[0]
    assert got.place_id == "p" and got.n_matches == 5
    assert got.score == pytest.approx(expected.score)
    assert got.best.score == pytest.approx(expected.best.score)


def test_aggregate_by_place_sums_top_three_and_sorts():
    def m(pid: str, score: float) -> ReviewMatch:
        return ReviewMatch(score, pid, pid.upper(), rv(pid, f"t{score}"))

    out = aggregate_by_place([m("a", 0.9), m("b", 0.8), m("a", 0.5), m("a", 0.4), m("a", 0.3), m("b", 0.1)])
    assert [p.place_id for p in out] == ["a", "b"]
    assert out[0].score == pytest.approx(0.9 + 0.5 + 0.4) and out[0].n_matches == 4
    assert out[0].best.score == 0.9 and out[0].place_name == "A"
    assert out[1].score == pytest.approx(0.9)
    assert len(aggregate_by_place([m("a", 1), m("b", 0.5)], top=1)) == 1
    assert aggregate_by_place([]) == []


# --- cache -------------------------------------------------------------------------------------


def test_cache_second_build_embeds_nothing(corpus, tmp_path):
    cache = tmp_path / "run" / "semantic.json"
    first = FakeEmbedder()
    SemanticIndex(corpus, first, cache_path=cache)
    assert first.n_embedded == 8 and cache.exists()
    second = FakeEmbedder()
    idx = SemanticIndex(corpus, second, cache_path=cache)
    assert second.n_embedded == 0
    assert idx.search("grout", min_score=0.1)  # still works from cached vectors


def test_cache_embeds_only_missing(corpus, tmp_path):
    cache = tmp_path / "c.json"
    SemanticIndex(corpus, FakeEmbedder(), cache_path=cache)
    corpus[0].reviews.append(rv("a", "a brand new late review", 3))
    emb = FakeEmbedder()
    SemanticIndex(corpus, emb, cache_path=cache)
    assert emb.doc_calls == [["a brand new late review"]]


def test_cache_changed_embedder_name_reembeds(corpus, tmp_path):
    cache = tmp_path / "c.json"
    SemanticIndex(corpus, FakeEmbedder("m1"), cache_path=cache)
    other = FakeEmbedder("m2")
    SemanticIndex(corpus, other, cache_path=cache)
    assert other.n_embedded == 8


def test_cache_corrupt_file_is_rebuilt(corpus, tmp_path):
    cache = tmp_path / "c.json"
    cache.write_text("{not json at all", encoding="utf-8")
    emb = FakeEmbedder()
    SemanticIndex(corpus, emb, cache_path=cache)
    assert emb.n_embedded == 8
    assert isinstance(json.loads(cache.read_text(encoding="utf-8")), dict)
    again = FakeEmbedder()
    SemanticIndex(corpus, again, cache_path=cache)
    assert again.n_embedded == 0


def test_cache_wrong_shape_is_ignored(corpus, tmp_path):
    cache = tmp_path / "c.json"
    cache.write_text(json.dumps(["a", "list"]), encoding="utf-8")
    emb = FakeEmbedder()
    SemanticIndex(corpus, emb, cache_path=cache)
    assert emb.n_embedded == 8


def test_cache_written_only_at_given_path_atomically(corpus, tmp_path):
    cache = tmp_path / "sub" / "semantic.json"
    SemanticIndex(corpus, FakeEmbedder(), cache_path=cache)
    assert sorted(p.name for p in tmp_path.rglob("*") if p.is_file()) == ["semantic.json"]


def test_no_cache_path_writes_nothing(corpus, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    SemanticIndex(corpus, FakeEmbedder())
    assert list(tmp_path.iterdir()) == []


def test_duplicate_texts_are_embedded_once(tmp_path):
    places = [place("p", [rv("p", "grout"), rv("p", "grout"), rv("p", "late")])]
    emb = FakeEmbedder()
    SemanticIndex(places, emb, cache_path=tmp_path / "c.json")
    assert emb.n_embedded == 2


# --- FastEmbedEmbedder -------------------------------------------------------------------------


def test_default_model_is_multilingual():
    assert "multilingual" in DEFAULT_MODEL
    assert FastEmbedEmbedder().name == DEFAULT_MODEL


def test_fastembed_missing_gives_install_hint(monkeypatch):
    monkeypatch.setitem(sys.modules, "fastembed", None)  # makes `import fastembed` raise ImportError
    emb = FastEmbedEmbedder()
    with pytest.raises(SemanticUnavailable, match=re.escape("uv sync --extra semantic")):
        emb.embed(["x"])
    with pytest.raises(SemanticUnavailable, match=re.escape("uv sync --extra semantic")):
        emb.embed_query("x")


def test_semantic_unavailable_is_a_runtime_error():
    assert issubclass(SemanticUnavailable, RuntimeError)


def test_model_load_failure_names_the_model(monkeypatch):
    fake = types.ModuleType("fastembed")

    class TextEmbedding:
        def __init__(self, model_name: str, **kw) -> None:
            raise OSError("host unreachable")

    fake.TextEmbedding = TextEmbedding
    monkeypatch.setitem(sys.modules, "fastembed", fake)
    with pytest.raises(SemanticUnavailable) as err:
        FastEmbedEmbedder("some/model").embed(["x"])
    assert "some/model" in str(err.value) and "reachable" in str(err.value)
    assert isinstance(err.value.__cause__, OSError)


def test_fastembed_happy_path_with_fake_module(monkeypatch):
    fake = types.ModuleType("fastembed")
    seen: list[str] = []

    class TextEmbedding:
        def __init__(self, model_name: str, **kw) -> None:
            seen.append(model_name)

        def embed(self, texts):
            return ([float(len(t)), 1.0] for t in texts)

        def query_embed(self, text):
            return iter([[9.0, 9.0]])

    fake.TextEmbedding = TextEmbedding
    monkeypatch.setitem(sys.modules, "fastembed", fake)
    emb = FastEmbedEmbedder()
    assert emb.embed(["ab", "abc"]) == [[2.0, 1.0], [3.0, 1.0]]
    assert emb.embed_query("q") == [9.0, 9.0]
    assert seen == [DEFAULT_MODEL]  # model loaded once and reused
