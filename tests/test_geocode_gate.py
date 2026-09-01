"""Both geocoding routes are gated on the school's own voivodeship.

The old rule accepted anything inside Poland's bounding box, so a same-named
village anywhere in the country was a valid match — and, separately, the RSPO
register's own geotag was adopted verbatim with no regional check at all. These
pin down that a coordinate outside the voivodeship the exam data assigns the
school is dropped rather than written, by either route.

Synthetic polygons on purpose: the rule under test is "inside this shape", and
tying it to real boundaries would make a failure hard to read.
"""

import geocode_schools as gs
import pytest

# Two disjoint unit squares standing in for two voivodeships.
HERE = {'type': 'Polygon', 'coordinates': [[[20, 52], [21, 52], [21, 53], [20, 53], [20, 52]]]}
ELSEWHERE = {'type': 'Polygon', 'coordinates': [[[15, 49], [16, 49], [16, 50], [15, 50], [15, 49]]]}
VOIVODESHIPS = {'14': HERE, '18': ELSEWHERE}

INSIDE_HERE = (52.5, 20.5)      # (lat, lon)
INSIDE_ELSEWHERE = (49.5, 15.5)


def school(rspo=1, teryt='1425065'):
    return {'rspo': rspo, 'teryt': teryt, 'miejscowosc': 'Sulmice', 'ulica_nr': 'Szkolna 1'}


def run(monkeypatch, tmp_path, *, geotag, address=None):
    monkeypatch.setattr(gs, '_rspo_geotag', lambda rspo: geotag)
    monkeypatch.setattr(gs, 'geocode_address', lambda m, u, a, v: address)
    plan = gs.plan_geocoding([school()], [], limit=None)
    return gs.run_geocoding_loop(plan, 'agent', tmp_path / 'coords.csv', VOIVODESHIPS)


def test_a_geotag_inside_the_schools_own_voivodeship_is_written(monkeypatch, tmp_path):
    result = run(monkeypatch, tmp_path, geotag=INSIDE_HERE)
    assert result.rejected_geotags == []
    assert (result.rows[0]['latitude'], result.rows[0]['longitude']) == INSIDE_HERE


def test_a_geotag_in_another_voivodeship_is_rejected_and_named(monkeypatch, tmp_path):
    """The register and the exam data disagree; the school goes unmapped, and
    says so loudly enough to be investigated rather than silently relocated."""
    result = run(monkeypatch, tmp_path, geotag=INSIDE_ELSEWHERE)
    assert result.rows[0]['latitude'] == ''
    assert result.rows[0]['longitude'] == ''
    (rejected,) = result.rejected_geotags
    assert rejected['rspo'] == 1
    assert (rejected['latitude'], rejected['longitude']) == INSIDE_ELSEWHERE


def test_a_rejected_geotag_still_falls_through_to_the_address_geocoder(monkeypatch, tmp_path):
    result = run(monkeypatch, tmp_path, geotag=INSIDE_ELSEWHERE, address=INSIDE_HERE)
    assert (result.rows[0]['latitude'], result.rows[0]['longitude']) == INSIDE_HERE
    assert len(result.rejected_geotags) == 1


def test_an_unknown_teryt_prefix_stops_the_run(monkeypatch, tmp_path):
    """Geocoding against a region we cannot name is exactly the failure this
    change exists to prevent, so it must not degrade to 'skip the check'."""
    monkeypatch.setattr(gs, '_rspo_geotag', lambda rspo: INSIDE_HERE)
    plan = gs.plan_geocoding([school(teryt='9925065')], [], limit=None)
    with pytest.raises(KeyError, match="'99'"):
        gs.run_geocoding_loop(plan, 'agent', tmp_path / 'coords.csv', VOIVODESHIPS)


def test_geocode_address_skips_a_hit_outside_the_voivodeship(monkeypatch):
    """Nominatim's viewbox is a soft bias, so the polygon test is the real gate:
    the first strategy's hit lands elsewhere and the second one's is taken."""
    replies = [
        [{'lat': '49.5', 'lon': '15.5'}],   # elsewhere — must be refused
        [{'lat': '52.5', 'lon': '20.5'}],   # inside — must be accepted
    ]
    seen = []

    def fake_request(params, user_agent):
        seen.append(params)
        return replies.pop(0) if replies else []

    monkeypatch.setattr(gs, '_nominatim_request', fake_request)
    assert gs.geocode_address('Sulmice', 'ul. Szkolna 1', 'agent', HERE) == (52.5, 20.5)
    assert len(seen) == 2
    # Every query is bounded to the voivodeship's own box, never to Poland's…
    assert all(p['viewbox'] == '20,53,21,52' and p['bounded'] == '1' for p in seen)
    # …and the region is never asserted in words (CLAUDE.md).
    assert not any('Mazowieckie' in str(p) for p in seen)


def test_geocode_address_returns_none_when_every_hit_is_outside(monkeypatch):
    monkeypatch.setattr(gs, '_nominatim_request', lambda p, a: [{'lat': '49.5', 'lon': '15.5'}])
    assert gs.geocode_address('Sulmice', 'ul. Szkolna 1', 'agent', HERE) is None
