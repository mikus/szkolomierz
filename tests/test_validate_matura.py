"""The validator's own EM2023 header parser and population rule (matura spec §2.3, §3.1).

These restate the pipeline's rules independently, so they get their own tests: a
validator whose parser silently merged two columns would agree with nothing and
explain nothing.
"""

import math

import pandas as pd
import pytest
from validate_export import classify_matura_row, matura_header_keys

NAN = math.nan


def header(*columns):
    """Three header rows from (band, subject, measure) column triples."""
    return pd.DataFrame([list(row) for row in zip(*columns, strict=True)])


def test_columns_before_the_first_band_cell_are_meta():
    top = header((NAN, NAN, 'RSPO szkoły'), (NAN, NAN, 'typ placówki\n'),
                 ('egzaminy obowiazkowe', 'matematyka poziom podstawowy (M)', 'średni wynik (%)'))
    assert matura_header_keys(top) == [
        ('meta', '', 'rspo szkoly'),
        ('meta', '', 'typ placowki'),
        ('egzaminy obowiazkowe', 'matematyka poziom podstawowy (m)', 'sredni wynik (%)'),
    ]


def test_the_2025_identifier_column_is_one_more_meta_column():
    top = header((NAN, NAN, 'RSPO szkoły'), (NAN, NAN, 'Identyfikator szkoły'),
                 ('.', 'dla całego egzaminu dojrzałości', '* zdawalność (%)'))
    assert matura_header_keys(top)[:2] == [('meta', '', 'rspo szkoly'),
                                           ('meta', '', 'identyfikator szkoly')]


def test_band_and_subject_fill_forward_and_the_star_is_dropped():
    top = header(('egzaminy obowiazkowe', 'język polski poziom podstawowy (M)',
                  '* liczba zdających'),
                 (NAN, NAN, 'średni wynik (%)'),
                 (NAN, 'matematyka poziom podstawowy (M)', 'mediana (%)'))
    assert matura_header_keys(top) == [
        ('egzaminy obowiazkowe', 'jezyk polski poziom podstawowy (m)', 'liczba zdajacych'),
        ('egzaminy obowiazkowe', 'jezyk polski poziom podstawowy (m)', 'sredni wynik (%)'),
        ('egzaminy obowiazkowe', 'matematyka poziom podstawowy (m)', 'mediana (%)'),
    ]


def test_one_subject_in_two_bands_gives_two_keys():
    oral = 'język angielski ustny (M)'
    top = header(('egzaminy obowiazkowe', oral, 'liczba zdających'),
                 ('egzaminy dodatkowe', oral, 'liczba zdających'))
    first, second = matura_header_keys(top)
    assert first[1:] == second[1:]
    assert first != second


def test_an_unsuffixed_subject_stays_apart_from_its_m_namesake():
    top = header(('egzaminy obowiazkowe', 'matematyka poziom podstawowy', 'średni wynik (%)'),
                 (NAN, 'matematyka poziom podstawowy (M)', 'średni wynik (%)'))
    keys = matura_header_keys(top)
    assert keys[0][1] == 'matematyka poziom podstawowy'
    assert keys[1][1] == 'matematyka poziom podstawowy (m)'


def test_blank_trailing_columns_are_keyed_empty_and_tolerated():
    top = header(('egzaminy obowiazkowe', 'matematyka poziom podstawowy (M)', 'mediana (%)'),
                 (NAN, NAN, NAN), (NAN, NAN, NAN))
    assert matura_header_keys(top)[1:] == [('', '', ''), ('', '', '')]


def test_a_repeated_key_exits():
    top = header(('egzaminy obowiazkowe', 'matematyka poziom podstawowy (M)', 'mediana (%)'),
                 (NAN, NAN, 'mediana (%)'))
    with pytest.raises(SystemExit):
        matura_header_keys(top)


@pytest.mark.parametrize(('typ', 'rodzaj', 'year', 'basic', 'expected'), [
    ('liceum', '', 2024, True, 'oke_pseudo_row'),
    ('technikum', NAN, 2026, True, 'oke_pseudo_row'),
    ('branzowa', 'dla młodzieży', 2024, True, 'branzowa_ii'),
    ('liceum', 'dla dorosłych', 2024, True, 'adult_liceum'),
    ('liceum', 'dla młodzieży', 2023, True, 'liceum'),
    ('liceum', 'nie dotyczy', 2025, True, 'liceum'),
    ('technikum', 'dla młodzieży', 2024, True, 'technikum'),
    ('technikum', 'dla młodzieży', 2023, False, 'technikum_2023'),
    ('liceum', 'dla młodzieży', 2024, False, 'missing_basic_subject'),
    ('liceum', 'dla młodzieży', 2027, True, 'liceum'),
    ('technikum', 'dla młodzieży', 2027, True, 'technikum'),
])
def test_population_rule(typ, rodzaj, year, basic, expected):
    assert classify_matura_row(typ, rodzaj, year, basic) == expected


def test_an_unknown_category_raises():
    with pytest.raises(ValueError, match='unknown school category'):
        classify_matura_row('technikum', 'dla dorosłych', 2024, True)


def coverage_case(source_years, published_years):
    """Check V on a minimal population: basic subjects only, every scale entry complete."""
    from pathlib import Path

    from validate_export import (
        COMPOSITE_INPUTS,
        EXPORT_METRICS,
        Profile,
        Report,
        check_matura_coverage,
    )

    subjects = (*COMPOSITE_INPUTS, 'composite_min')
    profile = Profile(name='matura/test', docs_data=Path('.'), subjects=subjects,
                      reference_levels=('national', 'voivodeship'),
                      region_levels=('voivodeship', 'powiat'), slider_subjects=subjects,
                      file_budgets_kb={}, shard_budget_kb=1, sparse_subjects=True)
    rows = [{'rspo': 1, 'year': year,
             **{s: {'n': 10.0, 'mean': 50.0, 'median': 50.0} for s in COMPOSITE_INPUTS}}
            for year in source_years]
    entry = {'sigma': 1.0, 'sigma_centre': 0.0, 'p1': -1.0, 'p99': 1.0}
    scale = {
        'metadata': {'subjects': list(subjects),
                     'subject_groups': {'basic': list(subjects), 'extended': []},
                     'levels': ['national', 'voivodeship'],
                     'years_in_data': list(published_years)},
        'school': {level: {metric: dict.fromkeys(subjects, entry) for metric in EXPORT_METRICS}
                   for level in ('national', 'voivodeship')},
    }
    rep = Report()
    check_matura_coverage(profile, rows, scale, rep)
    return rep.failures


def test_coverage_passes_when_the_published_years_are_the_source_years():
    assert coverage_case([2023, 2024, 2025, 2026], [2023, 2024, 2025, 2026]) == 0


def test_coverage_fails_when_the_export_drops_a_source_year():
    """Matura spec §9.1: each population's years come from the source files. An
    export that silently lost 2023 must fail, not narrow the recount to match."""
    assert coverage_case([2023, 2024, 2025, 2026], [2024, 2025, 2026]) == 1
