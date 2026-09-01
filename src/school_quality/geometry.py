"""Point-in-polygon over GeoJSON boundary geometry.

The geocoder needs to ask one question of a candidate coordinate: does it fall
inside the administrative unit the exam data assigns this school to? A bounding
box cannot answer it — Poland's voivodeships interlock, so a box around one
covers large parts of four others, which is how a same-named village 600 km away
passed for a match.

Pure geometry, no I/O: the caller reads the PRG boundary files (docs/geo/) and
hands the geometry dict over. Ray casting, with holes subtracted, which is the
same algorithm docs/map.js uses to resolve the map centre to a region — the two
must agree about which unit a point belongs to, or a school lands in a shard it
is not published in.

A point exactly on a boundary is undefined either way; the polygons here are
national borders and administrative lines, and no school sits on one to seven
decimal places.
"""


def _ring_contains(lon: float, lat: float, ring: list) -> bool:
    """Ray casting: count crossings of a horizontal ray to the west."""
    inside = False
    previous = len(ring) - 1
    for current in range(len(ring)):
        lon_i, lat_i = ring[current][0], ring[current][1]
        lon_j, lat_j = ring[previous][0], ring[previous][1]
        # The half-open latitude test is what keeps a vertex on the ray from
        # counting twice; it also makes the division below safe, because the two
        # latitudes cannot be equal once they straddle `lat`.
        if (lat_i > lat) != (lat_j > lat):
            crossing = (lon_j - lon_i) * (lat - lat_i) / (lat_j - lat_i) + lon_i
            if lon < crossing:
                inside = not inside
        previous = current
    return inside


def polygons(geometry: dict) -> list:
    """The Polygon list of a Polygon or MultiPolygon, as [[outer, *holes], …]."""
    kind = geometry['type']
    if kind == 'Polygon':
        return [geometry['coordinates']]
    if kind == 'MultiPolygon':
        return geometry['coordinates']
    raise ValueError(f'not an area geometry: {kind}')


def contains_point(geometry: dict, lon: float, lat: float) -> bool:
    """True if (lon, lat) falls inside `geometry`, holes excluded.

    Argument order is lon-then-lat, matching GeoJSON coordinates rather than the
    lat-then-lon this project's coordinate pairs use everywhere else. Keeping it
    in the geometry's own order means this function never has to swap, which is
    the mistake it would otherwise invite.
    """
    for rings in polygons(geometry):
        if not rings:
            continue
        if _ring_contains(lon, lat, rings[0]) and not any(
            _ring_contains(lon, lat, hole) for hole in rings[1:]
        ):
            return True
    return False


def bounding_box(geometry: dict) -> tuple[float, float, float, float]:
    """(lon_min, lat_min, lon_max, lat_max) over every ring of `geometry`."""
    lons: list[float] = []
    lats: list[float] = []
    for rings in polygons(geometry):
        for ring in rings:
            for lon, lat in ring:
                lons.append(lon)
                lats.append(lat)
    if not lons:
        raise ValueError('geometry has no coordinates')
    return min(lons), min(lats), max(lons), max(lats)
