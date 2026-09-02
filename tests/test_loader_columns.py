"""The metadata columns the loader must carry through from the national file."""

import pandas as pd
import pytest

from school_quality.teryt import gmina_key, normalise_teryt

REQUIRED_METADATA = [
    'rspo', 'year', 'school_name', 'is_public', 'gmina', 'powiat', 'typ_gminy',
    'miejscowosc', 'ulica_nr', 'wojewodztwo', 'teryt', 'id_oke', 'rodzaj_placowki',
]


def _row(**overrides):
    base = {
        'rspo': 2880, 'year': 2025, 'school_name': 'SP 1', 'is_public': 'Tak',
        'gmina': 'Jedlnia-Letnisko', 'powiat': 'Radomski', 'typ_gminy': 'Gmina wiejska',
        'miejscowosc': 'Słupica', 'ulica_nr': '84', 'wojewodztwo': 'Mazowieckie',
        'teryt': '1425063', 'id_oke': '7', 'rodzaj_placowki': 'dla młodzieży',
    }
    base.update(overrides)
    return base


def test_every_required_metadata_column_is_present():
    df = pd.DataFrame([_row()])
    assert set(REQUIRED_METADATA) <= set(df.columns)


def test_teryt_survives_as_a_seven_character_string():
    # A silent int cast would drop the leading zero on 0401011 and break the
    # voivodeship key for every school in kujawsko-pomorskie.
    df = pd.DataFrame([_row(teryt='0401011')])
    assert normalise_teryt(df.loc[0, 'teryt']) == '0401011'
    assert gmina_key(df.loc[0, 'teryt']) == '040101'


def test_a_missing_teryt_is_an_error_not_a_silent_drop():
    with pytest.raises(ValueError):
        normalise_teryt(None)
