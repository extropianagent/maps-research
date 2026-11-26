import pytest

from maps.geography import BBox, haversine_m

RIYADH = BBox(24.5, 46.5, 24.9, 46.9)


def test_parse_reads_south_west_north_east():
    assert BBox.parse("24.5,46.5,24.9,46.9") == RIYADH


def test_parse_rejects_wrong_arity():
    with pytest.raises(ValueError):
        BBox.parse("1,2,3")


def test_inverted_box_is_rejected():
    with pytest.raises(ValueError):
        BBox(24.9, 46.5, 24.5, 46.9)


def test_split_gives_four_quadrants_that_tile_the_parent():
    quads = RIYADH.split()
    assert len(quads) == 4
    assert min(q.south for q in quads) == RIYADH.south
    assert max(q.north for q in quads) == RIYADH.north
    assert min(q.west for q in quads) == RIYADH.west
    assert max(q.east for q in quads) == RIYADH.east
    area = sum((q.north - q.south) * (q.east - q.west) for q in quads)
    assert area == pytest.approx((RIYADH.north - RIYADH.south) * (RIYADH.east - RIYADH.west))


def test_every_point_in_parent_is_in_some_quadrant():
    for lat, lng in [(24.5, 46.5), (24.7, 46.7), (24.9, 46.9), (24.61, 46.88)]:
        assert any(q.contains(lat, lng) for q in RIYADH.split())


def test_contains_is_false_for_missing_coordinates():
    assert not RIYADH.contains(None, 46.7)


def test_around_encloses_the_requested_radius():
    box = BBox.around(51.5, -0.12, 1000)
    assert box.height_m == pytest.approx(2000, rel=0.01)
    assert box.width_m == pytest.approx(2000, rel=0.01)
    assert box.contains(51.5, -0.12)


def test_width_shrinks_with_latitude():
    equator = BBox(0, 0, 1, 1)
    north = BBox(60, 0, 61, 1)
    assert north.width_m < equator.width_m * 0.55


def test_haversine_one_degree_of_latitude():
    assert haversine_m(0, 0, 1, 0) == pytest.approx(111_195, rel=0.001)


def test_key_is_stable_and_distinguishes_quadrants():
    keys = {q.key() for q in RIYADH.split()}
    assert len(keys) == 4
    assert RIYADH.key() == "24.50000,46.50000,24.90000,46.90000"


def test_polygon_is_a_closed_ring():
    ring = RIYADH.polygon()
    assert len(ring) == 5 and ring[0] == ring[-1]
