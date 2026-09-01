"""The manual-triage report must name every school that has no coordinates.

Its rows used to be derived from the coordinate cache, which cannot see a school
the cache has no row for — so such a school was missing from the triage list as
well as from the map, with nothing anywhere pointing at it. Exactly one school
(rspo 269571) was in that state. The report is driven by the school population
now, and these tests pin that down.
"""

import csv

from geocode_schools import write_unmapped_report


def read_report(path):
    with path.open(newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def school(rspo, miejscowosc='Sulmice', ulica_nr='Szkolna 1'):
    return {'rspo': rspo, 'miejscowosc': miejscowosc, 'ulica_nr': ulica_nr}


def cache_row(rspo, latitude='52.1', longitude='21.0'):
    return {'rspo': str(rspo), 'miejscowosc': 'Sulmice', 'ulica_nr': 'Szkolna 1',
            'latitude': latitude, 'longitude': longitude}


def test_school_absent_from_the_cache_is_reported(tmp_path):
    path = tmp_path / 'unmapped.csv'
    n = write_unmapped_report([school(1), school(2)], [cache_row(1)], path)
    assert n == 1
    rows = read_report(path)
    assert [r['rspo'] for r in rows] == ['2']


def test_address_and_search_url_come_from_the_index(tmp_path):
    """A school with no cache row has no cached address either, so the report
    has to read the address off the school it was given."""
    path = tmp_path / 'unmapped.csv'
    write_unmapped_report([school(269571, 'Opole', 'ul. Krakowska 37')], [], path)
    (row,) = read_report(path)
    assert row['miejscowosc'] == 'Opole'
    assert row['ulica_nr'] == 'ul. Krakowska 37'
    assert 'Krakowska' in row['google_maps_search']


def test_half_a_coordinate_counts_as_unmapped(tmp_path):
    path = tmp_path / 'unmapped.csv'
    rows = [cache_row(1, latitude='', longitude=''),
            cache_row(2, longitude=''),
            cache_row(3, latitude='')]
    n = write_unmapped_report([school(1), school(2), school(3)], rows, path)
    assert n == 3


def test_cache_row_for_a_school_no_longer_in_the_index_is_not_reported(tmp_path):
    """The report is a to-do list for the map, and a school the source data
    dropped is not on it."""
    path = tmp_path / 'unmapped.csv'
    n = write_unmapped_report([school(1)], [cache_row(1), cache_row(99, '', '')], path)
    assert n == 0
    assert read_report(path) == []
