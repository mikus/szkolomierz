"""Which level a zoom shows, and what that level is compared against."""

import pytest

from school_quality.zoom import (
    LEVELS,
    child_level_of,
    level_for_zoom,
    reference_level_for,
)


def test_the_four_levels_run_coarse_to_fine():
    assert LEVELS == ('country', 'voivodeship', 'powiat', 'gmina')


@pytest.mark.parametrize('zoom, level', [
    (0, 'country'), (5, 'country'), (7, 'country'),
    (8, 'voivodeship'), (9, 'voivodeship'),
    (10, 'powiat'), (11, 'powiat'),
    (12, 'gmina'), (16, 'gmina'), (19, 'gmina'),
])
def test_zoom_maps_to_a_level(zoom, level):
    # Thresholds derived from real extents: Poland fits at z~7.5, a powiat
    # fills the view at z~11.6, a gmina at z~12.9.
    assert level_for_zoom(zoom) == level


def test_a_fractional_zoom_lands_on_the_lower_level():
    # Leaflet reports fractional zooms when animating.
    assert level_for_zoom(7.9) == 'country'
    assert level_for_zoom(8.0) == 'voivodeship'


def test_each_level_is_compared_against_the_one_above():
    # This is what stops the map going flat: shading voivodeships by a score
    # computed against their own voivodeship puts every one at zero.
    assert reference_level_for('country') == 'national'
    assert reference_level_for('voivodeship') == 'voivodeship'
    assert reference_level_for('powiat') == 'powiat'


def test_the_deepest_level_defers_to_the_user():
    # At school level all four reference levels are available and the selector
    # decides, so the ladder does not.
    assert reference_level_for('gmina') is None


def test_a_level_draws_its_children():
    assert child_level_of('country') == 'voivodeship'
    assert child_level_of('voivodeship') == 'powiat'
    assert child_level_of('powiat') == 'gmina'


def test_the_deepest_level_draws_schools_not_regions():
    assert child_level_of('gmina') is None


def test_an_unknown_level_is_rejected():
    with pytest.raises(KeyError):
        reference_level_for('district')
