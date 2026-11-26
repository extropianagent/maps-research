from maps import render
from maps.models import Hit, Place, Review
from maps.scoring import Scored


def scored(name="Bakery A", **kw):
    place = Place(
        "p1",
        name=name,
        rating=4.8,
        review_count=12,
        primary_type="bakery",
        address="1 Main St",
        maps_url=kw.pop("maps_url", "https://maps.example/p1"),
        hits=[Hit("sim", "bakery", level=2, rank=3), Hit("sim", "مخبز", level=1, rank=1)],
    )
    return Scored(place, 4.56789, {"bayes": 4.56789})


def test_place_row_flattens_everything_an_agent_needs():
    row = render.place_row(scored(), index=1)
    assert row["rank"] == 1 and row["place_id"] == "p1" and row["reviews"] == 12
    assert row["score"] == 4.568 and row["parts"] == {"bayes": 4.568}
    assert row["type"] == "bakery" and row["sources"] == ["sim"]
    assert row["seen_by"] == ["bakery", "مخبز"]
    assert row["first_level"] == 1
    assert row["top_rank"] is None  # no level-0 hit: invisible to an ordinary search


def test_markdown_table_links_names_and_flags_hidden_places():
    md = render.to_markdown([render.place_row(scored(), index=1)])
    lines = md.splitlines()
    assert lines[0].startswith("| # | Name | Rating | Reviews | Score | Type | Hidden? |")
    assert "[Bakery A](https://maps.example/p1)" in lines[2]
    assert lines[2].rstrip().endswith("| yes |")


def test_markdown_escapes_pipes_and_skips_link_without_url():
    row = render.place_row(scored(name="A | B", maps_url=None), index=2)
    md = render.to_markdown([row])
    assert "A \\| B" in md and "](" not in md


def test_review_block_always_carries_attribution_fields():
    r = Review("p1", text="Great", rating=5, author="Ann", url="u", flag_url="f", language="en")
    block = render.review_block(r, "Bakery A")
    for key in ("rating", "text", "language", "published", "author", "url", "flag_url"):
        assert key in block
    assert block["flag_url"] == "f" and block["author"] == "Ann"
    bare = render.review_block(Review("p1", text="x"), "Bakery A")
    assert bare["url"] is None and "flag_url" in bare


def test_review_block_accepts_a_match():
    from maps.reviews import ReviewMatch

    m = ReviewMatch(1.5, "p1", "Bakery A", Review("p1", text="stale bread", author="Bo"))
    block = render.review_block(m, "Bakery A")
    assert block["text"] == "stale bread" and block["score"] == 1.5
