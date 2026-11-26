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


# --- baseline comparison ------------------------------------------------------------------------

import json  # noqa: E402

from maps.models import Place  # noqa: E402


def bp(name="", url="", pid=""):
    return evals.BaselinePlace(name=name, maps_url=url, place_id=pid)


def run_places(*specs):
    """specs: (place_id, name, maps_url) -> Place objects."""
    return [Place(pid, name=name, maps_url=url) for pid, name, url in specs]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  Al-Baik  Fast Food! ", "al baik fast food"),
        ("CAFÉ  Bateel", "café bateel"),
        ("The Loaf & Co.", "the loaf co"),
        ("", ""),
    ],
)
def test_normalise_name(raw, expected):
    assert evals.normalise_name(raw) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.google.com/maps/place/?q=place_id:ChIJabcdefghijk1", "ChIJabcdefghijk1"),
        ("https://www.google.com/maps/place/X/data=!4m2!3m1!1sChIJabcdefghijk1!8m2", "ChIJabcdefghijk1"),
        (
            "https://www.google.com/maps/search/?api=1&query=x&query_place_id=ChIJabcdefghijk1",
            "ChIJabcdefghijk1",
        ),
        ("https://maps.google.com/?cid=123456789", None),
        ("https://example.com/bakery", None),
        ("", None),
    ],
)
def test_place_id_from_url(url, expected):
    assert evals.place_id_from_url(url) == expected


def test_match_by_place_id_beats_a_conflicting_name():
    places = run_places(("ChIJaaaaaaaaaa1", "Alpha Bakery", ""), ("ChIJbbbbbbbbbb2", "Beta Bakery", ""))
    assert evals.find_match(bp(name="Beta Bakery", pid="ChIJaaaaaaaaaa1"), places) == 0


def test_match_by_id_embedded_in_the_url_then_by_name():
    places = run_places(("ChIJaaaaaaaaaa1", "Alpha Bakery", ""), ("ChIJbbbbbbbbbb2", "Beta Bakery", ""))
    url = "https://www.google.com/maps/place/?q=place_id:ChIJbbbbbbbbbb2"
    assert evals.find_match(bp(name="zzz", url=url), places) == 1
    assert evals.find_match(bp(name="  BETA  bakery!"), places) == 1
    assert evals.find_match(bp(name="Gamma Bakery"), places) is None


def test_the_place_id_inside_a_run_places_own_url_matches_too():
    places = run_places(
        ("ChIJaaaaaaaaaa1", "Alpha", "https://www.google.com/maps/place/?q=place_id:ChIJzzzzzzzzzz9")
    )
    assert evals.find_match(bp(pid="ChIJzzzzzzzzzz9"), places) == 0


def test_containment_needs_the_shorter_name_to_be_at_least_five_characters():
    places = run_places(
        ("ChIJaaaaaaaaaa1", "Al Rayan Traditional Bakery", ""), ("ChIJbbbbbbbbbb2", "Cafe Bateel Riyadh", "")
    )
    assert evals.find_match(bp(name="Rayan"), places) == 0  # 5 chars, contained
    assert (
        evals.find_match(bp(name="Cafe Bateel Riyadh Branch Two"), places) == 1
    )  # the run name is contained
    assert evals.find_match(bp(name="Ray"), places) is None  # too short
    assert evals.find_match(bp(name="Bat"), places) is None


def test_equality_beats_containment_and_ties_go_to_the_better_ranked():
    places = run_places(
        ("ChIJaaaaaaaaaa1", "Loaf House Downtown", ""),
        ("ChIJbbbbbbbbbb2", "Loaf House", ""),
        ("ChIJcccccccccc3", "Loaf House Mall", ""),
    )
    assert evals.find_match(bp(name="loaf house"), places) == 1
    assert evals.find_match(bp(name="Loaf"), places) is None  # 4 chars
    assert evals.find_match(bp(name="House Downtown"), places) == 0


def test_compare_overlap_only_toolkit_and_gap_kinds():
    ranked = run_places(
        ("ChIJaaaaaaaaaa1", "Alpha Bakery", ""),
        ("ChIJbbbbbbbbbb2", "Beta Bakery", ""),
        ("ChIJcccccccccc3", "Gamma Bakery", ""),
    )
    filtered_out = Place("ChIJdddddddddd4", name="Delta Bakery")
    baseline = evals.Baseline(
        "manual",
        "Maps by hand",
        [bp(name="Alpha Bakery"), bp(name="Gamma Bakery"), bp(name="Delta Bakery"), bp(name="Omega Bakery")],
    )
    res = evals.compare_to_baseline(baseline, ranked, [*ranked, filtered_out], top=2)
    assert [o["baseline"]["name"] for o in res["overlap"]] == ["Alpha Bakery"]
    assert [t["place_id"] for t in res["only_toolkit"]] == ["ChIJbbbbbbbbbb2"] and res["only_toolkit"][0][
        "rank"
    ] == 2
    gaps = {g["name"]: g["gap"] for g in res["only_baseline"]}
    assert gaps == {
        "Gamma Bakery": "retrieved, ranked #3",
        "Delta Bakery": "retrieved but filtered out",
        "Omega Bakery": "not retrieved",
    }
    assert res["counts"] == {
        "baseline": 4,
        "toolkit_top": 2,
        "overlap": 1,
        "only_toolkit": 1,
        "only_baseline": 3,
    }


def write(tmp_path, doc):
    p = tmp_path / "b.json"
    p.write_text(json.dumps(doc))
    return p


def test_load_baseline_accepts_partial_places(tmp_path):
    b = evals.load_baseline(
        write(
            tmp_path,
            {"name": "n", "source": "s", "places": [{"name": "A"}, {"maps_url": "u"}, {"place_id": "ChIJx"}]},
        )
    )
    assert (b.name, b.source, len(b.places)) == ("n", "s", 3)


@pytest.mark.parametrize(
    "doc",
    [
        [],
        {"name": "n", "source": "s"},
        {"name": "n", "source": "s", "places": []},
        {"name": "n", "source": "s", "places": [{"name": "", "maps_url": "", "place_id": ""}]},
        {"name": "n", "source": "s", "places": ["A"]},
        {"name": 3, "source": "s", "places": [{"name": "A"}]},
        {"name": "n", "source": "s", "places": [{"name": 5}]},
    ],
)
def test_load_baseline_rejects_bad_files(tmp_path, doc):
    with pytest.raises(ValueError):
        evals.load_baseline(write(tmp_path, doc))


def test_load_baseline_rejects_unreadable_files(tmp_path):
    with pytest.raises(ValueError):
        evals.load_baseline(tmp_path / "missing.json")
    (tmp_path / "bad.json").write_text("{not json")
    with pytest.raises(ValueError):
        evals.load_baseline(tmp_path / "bad.json")
