"""Reading a coordinate out of an RSPO institution payload."""

import pytest

from school_quality.rspo import geotag_from_payload, rspo_detail_url


def test_url_is_built_from_the_school_id():
    assert rspo_detail_url(52934).endswith('/api/Institution/52934')


def test_reads_a_present_geotag():
    payload = {'hqAddressGeotag': {'latitude': 52.87745, 'longitude': 18.69947}}
    assert geotag_from_payload(payload) == (52.87745, 18.69947)


def test_coerces_string_coordinates():
    # The documented API returns GeotagDTO with string latitude/longitude.
    payload = {'hqAddressGeotag': {'latitude': '52.87745', 'longitude': '18.69947'}}
    assert geotag_from_payload(payload) == (52.87745, 18.69947)


@pytest.mark.parametrize('payload', [
    {},
    {'hqAddressGeotag': None},
    {'hqAddressGeotag': {}},
    {'hqAddressGeotag': {'latitude': None, 'longitude': None}},
    {'hqAddressGeotag': {'latitude': '', 'longitude': ''}},
    {'hqAddressGeotag': 'some_string'},
    'a string payload',
    ['a', 'list', 'payload'],
    42,
])
def test_a_missing_geotag_is_none_not_an_error(payload):
    # A school without a geotag must fall through to the address geocoder,
    # not abort a 10,000-school run.
    assert geotag_from_payload(payload) is None


def test_a_non_numeric_geotag_is_none():
    assert geotag_from_payload({'hqAddressGeotag': {'latitude': 'n/a', 'longitude': '1'}}) is None


def test_coordinates_outside_poland_are_rejected():
    # 0,0 is the classic geocoder null island; it must not land on the map.
    assert geotag_from_payload({'hqAddressGeotag': {'latitude': 0, 'longitude': 0}}) is None
