from maps.models import Hit, Place, Query, Review


def test_query_parses_language_suffix():
    assert Query.parse("مخبز:ar") == Query("مخبز", "ar")
    assert Query.parse("tile installer:en-GB") == Query("tile installer", "en-GB")


def test_query_without_suffix_has_no_language():
    assert Query.parse("bakery") == Query("bakery", None)


def test_query_keeps_colon_that_is_not_a_language_tag():
    assert Query.parse("ratio: 16:9 screens") == Query("ratio: 16:9 screens", None)


def test_merge_fills_missing_scalars_but_keeps_existing_ones():
    a = Place("p1", name="Crumb", rating=None)
    b = Place("p1", name="Crumb Bakery", rating=4.7, review_count=31)
    a.merge(b)
    assert a.name == "Crumb"
    assert a.rating == 4.7
    assert a.review_count == 31


def test_merge_accumulates_hits_and_unions_types():
    a = Place("p1", types=["bakery"], hits=[Hit("google", "bakery", rank=3)])
    b = Place("p1", types=["bakery", "cafe"], hits=[Hit("apify", "مخبز", "ar", rank=1)])
    a.merge(b)
    assert a.types == ["bakery", "cafe"]
    assert [h.provider for h in a.hits] == ["google", "apify"]
    assert a.sources == ["apify", "google"]


def test_merge_does_not_duplicate_the_same_review():
    r = Review("p1", text="Best sourdough in town", rating=5, author="A", source="google")
    a = Place("p1", reviews=[r])
    b = Place(
        "p1", reviews=[Review.from_dict(r.to_dict()), Review("p1", text="Stale", rating=2, source="google")]
    )
    a.merge(b)
    assert [x.text for x in a.reviews] == ["Best sourdough in town", "Stale"]


def test_merge_rejects_a_different_place():
    import pytest

    with pytest.raises(ValueError):
        Place("p1").merge(Place("p2"))


def test_place_round_trips_through_dict():
    p = Place(
        "p1",
        name="Crumb",
        lat=24.7,
        lng=46.7,
        rating=4.6,
        hits=[Hit("google", "bakery", "en", "1,2,3,4", 2, 7)],
        reviews=[Review("p1", text="good", rating=4, source="google")],
    )
    assert Place.from_dict(p.to_dict()) == p


def test_from_dict_ignores_unknown_fields():
    assert Place.from_dict({"place_id": "p1", "future_field": 1}).place_id == "p1"


def test_merge_dedups_the_same_review_seen_through_two_providers():
    def reviews(source):
        return [
            Review(
                "p1",
                text=f"Review number {i}  with   spacing",
                rating=4,
                author=f" Author {i} ",
                published=f"2024-01-0{i + 1}T10:00:00Z",
                source=source,
            )
            for i in range(5)
        ]

    a = Place("p1", reviews=reviews("google"))
    b = Place(
        "p1",
        reviews=[
            Review(
                "p1",
                text=r.text.upper(),
                rating=r.rating,
                author=r.author.strip().lower(),
                published=None,
                source="apify",
            )
            for r in reviews("apify")
        ],
    )
    a.merge(b)
    assert len(a.reviews) == 5


def test_merge_keeps_distinct_textless_reviews_and_dedups_identical_ones():
    a = Place(
        "p1",
        reviews=[Review("p1", rating=5, author="Ann", published="2024-05-01T00:00:00Z", source="google")],
    )
    b = Place(
        "p1",
        reviews=[
            Review("p1", rating=5, author="Bob", published="2024-05-01T09:00:00Z", source="apify"),
            Review("p1", rating=5, author="ann", published="2024-05-01T12:00:00Z", source="apify"),
        ],
    )
    a.merge(b)
    assert sorted((r.author or "").lower() for r in a.reviews) == ["ann", "bob"]
