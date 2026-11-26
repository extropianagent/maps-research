from maps.filters import Filters, apply
from maps.geography import BBox
from maps.models import Place

PLACES = [
    Place("a", name="Alpha Tiles", rating=4.5, review_count=10, types=["store"], primary_type="store"),
    Place("b", name="Beta Tiling", rating=4.6, review_count=200, types=["contractor"]),
    Place("c", name="Gamma", rating=3.9, review_count=9, types=["contractor", "store"]),
    Place("d", name="Unrated", rating=None, review_count=None, types=["contractor"]),
    Place("e", name="Closed Co", rating=4.4, review_count=50, business_status="CLOSED_PERMANENTLY"),
]


def ids(f):
    return [p.place_id for p in apply(PLACES, f)]


def test_no_filters_keeps_everything():
    assert ids(Filters()) == ["a", "b", "c", "d", "e"]


def test_max_rating_is_inclusive_and_exact():
    assert ids(Filters(max_rating=4.5)) == ["a", "c", "e"]


def test_min_rating_is_inclusive():
    assert ids(Filters(min_rating=4.5)) == ["a", "b"]


def test_rating_bounds_exclude_places_with_unknown_rating():
    assert "d" not in ids(Filters(min_rating=0))
    assert "d" not in ids(Filters(max_rating=5))


def test_review_count_bounds_are_inclusive_and_exclude_unknown():
    assert ids(Filters(min_reviews=10)) == ["a", "b", "e"]
    assert ids(Filters(max_reviews=10)) == ["a", "c"]


def test_combined_bounds_from_the_tile_installer_example():
    assert ids(Filters(max_rating=4.5, min_reviews=10)) == ["a", "e"]


def test_types_matches_any_listed_type_including_primary():
    assert ids(Filters(types=["store"])) == ["a", "c"]


def test_exclude_types_removes_any_match():
    assert ids(Filters(exclude_types=["store"])) == ["b", "d", "e"]


def test_open_only_drops_permanently_closed_places():
    assert "e" not in ids(Filters(open_only=True))


def test_name_regex_is_case_insensitive():
    assert ids(Filters(name_regex="til(es|ing)")) == ["a", "b"]


def test_bbox_drops_places_outside_or_without_coordinates():
    inside = Place("in", lat=1.5, lng=1.5)
    outside = Place("out", lat=5, lng=5)
    unknown = Place("unk")
    kept = apply([inside, outside, unknown], Filters(bbox=BBox(1, 1, 2, 2)))
    assert [p.place_id for p in kept] == ["in"]
