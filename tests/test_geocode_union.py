"""The geocoder reads every school index into one population (matura spec §8.6).

One shared coordinate cache serves both exams, so the population it is planned,
forced and triaged over has to be the union: a run that saw only one exam's
index would leave the other's schools out of the unmapped report, and --force,
which starts from an empty cache, would drop their coordinates altogether.
"""

import json

import pytest
from geocode_schools import load_schools


def write_index(path, *schools):
    columns = {key: [school[key] for school in schools]
               for key in ('rspo', 'teryt', 'miejscowosc', 'ulica_nr')}
    path.write_text(json.dumps({'schools': columns}), encoding='utf-8')


def school(rspo, ulica_nr='Szkolna 1'):
    return {'rspo': rspo, 'teryt': '1465011', 'miejscowosc': 'Warszawa', 'ulica_nr': ulica_nr}


def test_every_index_contributes_its_schools(tmp_path):
    write_index(tmp_path / 'e8.json', school(1), school(2))
    write_index(tmp_path / 'liceum.json', school(3))
    write_index(tmp_path / 'technikum.json', school(4))
    paths = [tmp_path / name for name in ('e8.json', 'liceum.json', 'technikum.json')]
    assert [s['rspo'] for s in load_schools(paths)] == [1, 2, 3, 4]


def test_a_school_in_two_indexes_keeps_the_first_index_row(tmp_path):
    """The 37 music and ballet schools are in E8's index and matura's: E8's row,
    and so the address already geocoded, is the one kept."""
    write_index(tmp_path / 'e8.json', school(1, 'ul. Szkolna 1'))
    write_index(tmp_path / 'liceum.json', school(1, 'Szkolna 1'), school(2))
    schools = load_schools([tmp_path / 'e8.json', tmp_path / 'liceum.json'])
    assert [s['rspo'] for s in schools] == [1, 2]
    assert schools[0]['ulica_nr'] == 'ul. Szkolna 1'


def test_a_missing_index_raises(tmp_path):
    write_index(tmp_path / 'e8.json', school(1))
    with pytest.raises(FileNotFoundError):
        load_schools([tmp_path / 'e8.json', tmp_path / 'absent.json'])
