"""Point-in-polygon, the accept rule the geocoder gates candidate coordinates on.

The case that matters is the concave one: a bounding box around a voivodeship
covers large parts of its neighbours, so a box test accepts a same-named village
hundreds of km away. These pin down that the polygon test does not.
"""

import pytest

from school_quality.geometry import bounding_box, contains_point, polygons

SQUARE = {'type': 'Polygon', 'coordinates': [[[0, 0], [4, 0], [4, 4], [0, 4], [0, 0]]]}

# A square with its north-east quarter bitten out. Its bounding box is still the
# whole square, so (3, 3) is inside the box and outside the shape.
L_SHAPE = {
    'type': 'Polygon',
    'coordinates': [[[0, 0], [4, 0], [4, 2], [2, 2], [2, 4], [0, 4], [0, 0]]],
}

SQUARE_WITH_HOLE = {
    'type': 'Polygon',
    'coordinates': [
        [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]],
        [[4, 4], [6, 4], [6, 6], [4, 6], [4, 4]],
    ],
}

TWO_PARTS = {
    'type': 'MultiPolygon',
    'coordinates': [
        [[[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]],
        [[[8, 8], [10, 8], [10, 10], [8, 10], [8, 8]]],
    ],
}


def test_inside_and_outside_a_simple_polygon():
    assert contains_point(SQUARE, 2, 2)
    assert not contains_point(SQUARE, 5, 2)
    assert not contains_point(SQUARE, 2, -1)


def test_a_point_in_the_bounding_box_but_outside_the_shape_is_rejected():
    """The whole reason the accept rule is a polygon test and not a box test."""
    lon_min, lat_min, lon_max, lat_max = bounding_box(L_SHAPE)
    assert lon_min <= 3 <= lon_max and lat_min <= 3 <= lat_max
    assert not contains_point(L_SHAPE, 3, 3)
    assert contains_point(L_SHAPE, 1, 3)


def test_a_hole_is_not_inside():
    assert contains_point(SQUARE_WITH_HOLE, 2, 2)
    assert not contains_point(SQUARE_WITH_HOLE, 5, 5)


def test_either_part_of_a_multipolygon_counts():
    assert contains_point(TWO_PARTS, 1, 1)
    assert contains_point(TWO_PARTS, 9, 9)
    assert not contains_point(TWO_PARTS, 5, 5)


def test_bounding_box_spans_every_part():
    assert bounding_box(TWO_PARTS) == (0, 0, 10, 10)
    assert bounding_box(SQUARE_WITH_HOLE) == (0, 0, 10, 10)


def test_a_vertex_on_the_ray_is_counted_once():
    """A ray due west from (5, 4) leaves through the [4, 4] corner. Counting that
    corner twice — or not at all — flips the answer for a whole latitude band."""
    assert contains_point(SQUARE, 3, 4 - 1e-9)
    assert not contains_point(SQUARE, 5, 4 - 1e-9)


def test_polygons_rejects_a_non_area_geometry():
    with pytest.raises(ValueError, match='LineString'):
        polygons({'type': 'LineString', 'coordinates': [[0, 0], [1, 1]]})
