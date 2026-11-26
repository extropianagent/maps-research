import pytest

from maps import evals
from maps.retrieval import DEPTHS


@pytest.fixture(scope="module")
def rows():
    return evals.sim_benchmark()


def test_one_row_per_depth_in_order(rows):
    assert [r["depth"] for r in rows] == list(DEPTHS)
    assert set(rows[0]) == {"depth", "recall", "recall_en_only", "gem_recall", "searches"}


def test_recall_grows_with_depth(rows):
    r = {row["depth"]: row["recall"] for row in rows}
    assert r["quick"] < r["standard"] < r["deep"] <= r["exhaustive"]


def test_exhaustive_is_nearly_complete(rows):
    assert rows[-1]["recall"] >= 0.97


def test_multilingual_expansion_beats_english_alone_at_every_depth(rows):
    assert all(row["recall_en_only"] < row["recall"] for row in rows)


def test_values_are_rounded_to_three_decimals(rows):
    for row in rows:
        for key in ("recall", "recall_en_only", "gem_recall", "searches"):
            assert row[key] == round(row[key], 3)


def test_expected_recall_arithmetic():
    out = evals.expected_recall(["a", "b", "x"], ["a", "b", "c", "d"])
    assert out == {"expected": 4, "found": 2, "recall": 0.5, "missing": ["c", "d"]}


def test_expected_recall_with_nothing_expected_is_perfect():
    assert evals.expected_recall(["a"], []) == {"expected": 0, "found": 0, "recall": 1.0, "missing": []}


def test_default_benchmark_world_is_big_enough_to_separate_depths():
    import inspect

    assert inspect.signature(evals.sim_benchmark).parameters["n_places"].default == 3000
