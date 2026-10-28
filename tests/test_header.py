"""The three-row CIE header (sheet SAS) parsed into (band, subject, measure) keys.

Labels below are the raw cells of the real EM2023 files, diacritics, `(M)`
suffixes, leading `*` and trailing newlines included.
"""

import re

import pandas as pd
import pytest

from school_quality.header import apply_header, normalize_label, parse_header

# The 13 meta columns of every 2023-2024 file and every July edition.
META_13 = [
    (None, None, 'ID OKE'),
    (None, None, 'Województwo - nazwa'),
    (None, None, 'Powiat - nazwa'),
    (None, None, 'Gmina - nazwa'),
    (None, None, 'Typ gminy'),
    (None, None, 'Kod teryt gminy'),
    (None, None, 'RSPO szkoły'),
    (None, None, 'Nazwa szkoły'),
    (None, None, 'Miejscowość'),
    (None, None, 'Ulica nr'),
    (None, None, 'typ placówki\n'),
    (None, None, 'rodzaj placówki\n'),
    (None, None, 'czy publiczna'),
]
# September 2025 and 2026 add `Identyfikator szkoły` after `RSPO szkoły`.
META_14 = [*META_13[:7], (None, None, 'Identyfikator szkoły'), *META_13[7:]]
# A short meta block for the tests that are about the data columns.
META = [
    (None, None, 'RSPO szkoły'),
    (None, None, 'typ placówki\n'),
    (None, None, 'czy publiczna'),
]
WHOLE_EXAM = [
    ('.', 'dla całego egzaminu dojrzałości', '* Otrzymali świadectwo dojrzałości - liczba'),
    (None, None, '* zdawalność (%)'),
]
POLISH_BASIC = [
    ('egzaminy obowiazkowe', 'język polski poziom podstawowy (M)', '* liczba zdających'),
    (None, None, 'średni wynik (%)'),
]
POLISH = ('egzaminy obowiazkowe', 'jezyk polski poziom podstawowy (m)')


def _top(*columns):
    """A sheet's first three rows, from one (row 0, row 1, row 2) triple per column."""
    return pd.DataFrame(list(zip(*columns)))


@pytest.mark.parametrize(('raw', 'label'), [
    ('język polski poziom podstawowy (M)', 'jezyk polski poziom podstawowy (m)'),
    ('Województwo - nazwa', 'wojewodztwo - nazwa'),
    # ł has no NFKD decomposition, so it is mapped by hand.
    ('język łaciński i kultura antyczna poziom rozszerzony (M)',
     'jezyk lacinski i kultura antyczna poziom rozszerzony (m)'),
    ('Łódź', 'lodz'),
    ('typ placówki\n', 'typ placowki'),
    ('  dla   całego\negzaminu  ', 'dla calego egzaminu'),
    ('* liczba zdających', 'liczba zdajacych'),
    ('* Liczba laureatów/finalistów', 'liczba laureatow/finalistow'),
    ('* zdawalność (%)', 'zdawalnosc (%)'),
])
def test_normalize_label_gives_ascii_lowercase_single_spaced_text(raw, label):
    assert normalize_label(raw) == label


@pytest.mark.parametrize('blank', [None, float('nan'), 'Unnamed: 3_level_0'])
def test_normalize_label_maps_a_blank_cell_to_the_empty_string(blank):
    assert normalize_label(blank) == ''


def test_columns_before_the_first_band_cell_are_meta_keyed_by_their_row_2_label():
    keys = parse_header(_top(*META, *WHOLE_EXAM))
    assert keys == [
        ('meta', '', 'rspo szkoly'),
        ('meta', '', 'typ placowki'),
        ('meta', '', 'czy publiczna'),
        ('.', 'dla calego egzaminu dojrzalosci', 'otrzymali swiadectwo dojrzalosci - liczba'),
        ('.', 'dla calego egzaminu dojrzalosci', 'zdawalnosc (%)'),
    ]


def test_band_and_subject_carry_forward_until_a_cell_names_a_new_one():
    keys = parse_header(_top(
        *META,
        *POLISH_BASIC,
        (None, 'matematyka poziom podstawowy (M)', '* liczba zdających'),
        (None, None, 'mediana (%)'),
    ))
    assert keys[3:] == [
        (*POLISH, 'liczba zdajacych'),
        (*POLISH, 'sredni wynik (%)'),
        ('egzaminy obowiazkowe', 'matematyka poziom podstawowy (m)', 'liczba zdajacych'),
        ('egzaminy obowiazkowe', 'matematyka poziom podstawowy (m)', 'mediana (%)'),
    ]


def test_a_trailing_column_with_blank_header_cells_raises_rather_than_join_the_last_subject():
    # Forward-filled, it would silently become a third measure of basic Polish.
    top = _top(*META, *POLISH_BASIC, (None, None, None))
    with pytest.raises(ValueError, match='header column 5 has a blank row-2 label'):
        parse_header(top)


def test_a_meta_column_with_a_blank_label_raises():
    top = _top(META[0], (None, None, None), *META[1:], *POLISH_BASIC)
    with pytest.raises(ValueError, match='header column 1 has a blank row-2 label'):
        parse_header(top)


def test_a_new_band_does_not_inherit_the_previous_bands_subject():
    top = _top(*META, *POLISH_BASIC, ('egzaminy dodatkowe', None, '* liczba zdających'))
    with pytest.raises(ValueError, match="header column 5 .*'egzaminy dodatkowe'.* no subject"):
        parse_header(top)


def test_a_subject_in_two_bands_gets_two_distinct_keys():
    # `język angielski ustny (M)` is in both bands of every EM2023 file.
    keys = parse_header(_top(
        *META,
        ('egzaminy obowiazkowe', 'język angielski ustny (M)', '* liczba zdających'),
        ('egzaminy dodatkowe', 'język angielski ustny (M)', '* liczba zdających'),
    ))
    assert keys[3:] == [
        ('egzaminy obowiazkowe', 'jezyk angielski ustny (m)', 'liczba zdajacych'),
        ('egzaminy dodatkowe', 'jezyk angielski ustny (m)', 'liczba zdajacych'),
    ]


def test_an_unsuffixed_subject_and_its_m_namesake_stay_distinct():
    # The unsuffixed one is maths sat in Lithuanian at one liceum in Puńsk: a
    # different exam, which stripping the suffix would merge into ordinary maths.
    keys = parse_header(_top(
        *META,
        ('egzaminy obowiazkowe', 'matematyka poziom podstawowy (M)', 'średni wynik (%)'),
        (None, 'matematyka poziom podstawowy', 'średni wynik (%)'),
    ))
    assert keys[3:] == [
        ('egzaminy obowiazkowe', 'matematyka poziom podstawowy (m)', 'sredni wynik (%)'),
        ('egzaminy obowiazkowe', 'matematyka poziom podstawowy', 'sredni wynik (%)'),
    ]


def test_the_2025_identifier_column_is_one_more_meta_column_and_moves_no_data_key():
    keys_13 = parse_header(_top(*META_13, *WHOLE_EXAM, *POLISH_BASIC))
    keys_14 = parse_header(_top(*META_14, *WHOLE_EXAM, *POLISH_BASIC))
    meta_13 = [key for key in keys_13 if key[0] == 'meta']
    meta_14 = [key for key in keys_14 if key[0] == 'meta']
    assert len(meta_13) == 13
    assert len(meta_14) == 14
    assert meta_14[6:9] == [
        ('meta', '', 'rspo szkoly'),
        ('meta', '', 'identyfikator szkoly'),
        ('meta', '', 'nazwa szkoly'),
    ]
    assert keys_14[14:] == keys_13[13:]


def test_a_duplicate_key_raises_and_names_it():
    top = _top(*META, *POLISH_BASIC, (None, None, 'średni wynik (%)'))
    duplicate = repr((*POLISH, 'sredni wynik (%)'))
    with pytest.raises(ValueError, match=re.escape(duplicate)):
        parse_header(top)


def test_apply_header_returns_the_data_rows_under_the_parsed_keys():
    raw = pd.DataFrame([
        [None, '.', None],
        [None, 'dla całego egzaminu dojrzałości', None],
        ['RSPO szkoły', '* Otrzymali świadectwo dojrzałości - liczba', '* zdawalność (%)'],
        [12325, 16, 76.25],
        [91402, None, None],
    ])
    out = apply_header(raw)
    pass_rate = ('.', 'dla calego egzaminu dojrzalosci', 'zdawalnosc (%)')
    assert out.columns.names == ['band', 'subject', 'measure']
    assert list(out.columns) == [
        ('meta', '', 'rspo szkoly'),
        ('.', 'dla calego egzaminu dojrzalosci', 'otrzymali swiadectwo dojrzalosci - liczba'),
        pass_rate,
    ]
    assert out.index.equals(pd.RangeIndex(2))
    assert out[('meta', '', 'rspo szkoly')].tolist() == [12325, 91402]
    assert out[pass_rate].iloc[0] == 76.25
    assert pd.isna(out[pass_rate].iloc[1])
