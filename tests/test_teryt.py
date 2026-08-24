"""Region keys derived from the TERYT code of a school's gmina."""

import pytest

from school_quality.teryt import (
    gmina_key,
    normalise_teryt,
    powiat_key,
    voivodeship_key,
)


def test_normalises_a_seven_digit_code_unchanged():
    assert normalise_teryt('1425063') == '1425063'


def test_pads_a_code_that_lost_its_leading_zero():
    # Excel turns 0401011 into the number 401011.
    assert normalise_teryt(401011) == '0401011'


def test_accepts_a_float_from_pandas():
    assert normalise_teryt(1425063.0) == '1425063'


@pytest.mark.parametrize('bad', ['', None, 'abc', '142506312'])
def test_rejects_anything_that_is_not_a_teryt_code(bad):
    with pytest.raises(ValueError):
        normalise_teryt(bad)


def test_voivodeship_is_the_first_two_digits():
    assert voivodeship_key('1425063') == '14'


def test_powiat_is_the_first_four_digits():
    assert powiat_key('1425063') == '1425'


def test_gmina_is_the_first_six_digits_not_seven():
    # The 7th digit is the gmina TYPE, not identity. Including it splits a
    # gmina's history when a village becomes a town.
    assert gmina_key('1425063') == '142506'


def test_a_gmina_keeps_one_key_when_its_type_digit_changes():
    rural, urban_rural = '1425062', '1425065'
    assert gmina_key(rural) == gmina_key(urban_rural)
    assert rural != urban_rural


def test_the_two_halves_of_an_urban_rural_gmina_share_a_key():
    miasto, obszar_wiejski = '0218034', '0218035'
    assert gmina_key(miasto) == gmina_key(obszar_wiejski) == '021803'


def test_keys_nest():
    teryt = '1425063'
    assert gmina_key(teryt).startswith(powiat_key(teryt))
    assert powiat_key(teryt).startswith(voivodeship_key(teryt))
