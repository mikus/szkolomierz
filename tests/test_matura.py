"""EM2023 rules: the population classifier, the populations, and the subject key map."""

import pytest

from school_quality.header import normalize_label
from school_quality.matura import (
    ART_SCHOOL_RE,
    BASIC_BAND,
    BASIC_KEYS,
    BASIC_SUBJECTS,
    COMPOSITE,
    COVERAGE_MIN_LICEA,
    EXTENDED_BAND,
    EXTENDED_SUBJECTS,
    MEASURE_MEAN,
    MEASURE_MEDIAN,
    MEASURE_N,
    MEASURE_PASS_RATE,
    POPULATION_EXCLUSIONS,
    POPULATIONS,
    REJECT_REASONS,
    SUBJECT_KEYS,
    TECHNIKUM_FIRST_YEAR,
    WHOLE_EXAM,
    assert_art_school_names,
    assert_coverage_matches,
    assert_population_exclusions,
    assert_unsuffixed_unpublished,
    classify,
    export_subjects,
    in_population,
    passing_coverage,
    school_type,
    subject_groups,
    unsuffixed_subjects,
)

YOUTH = 'dla młodzieży'
ADULT = 'dla dorosłych'
ART = 'nie dotyczy'
NAN = float('nan')

EXTENDED_SOURCE = {subject for _, subject in EXTENDED_SUBJECTS.values()}


# --- the population classifier (spec §3.1) ---------------------------------------


@pytest.mark.parametrize(
    'typ, rodzaj, year, has_basic_means, expected',
    [
        ('liceum', YOUTH, 2025, True, 'liceum'),
        ('technikum', YOUTH, 2025, True, 'technikum'),
        ('liceum', ADULT, 2025, True, 'adult_liceum'),
        ('branzowa', ART, 2025, True, 'branzowa_ii'),
        ('technikum', YOUTH, 2023, True, 'technikum_2023'),
        ('liceum', YOUTH, 2025, False, 'missing_basic_subject'),
        ('technikum', YOUTH, 2024, False, 'missing_basic_subject'),
        ('liceum', '', 2025, True, 'oke_pseudo_row'),
    ],
)
def test_each_outcome_of_the_rule(typ, rodzaj, year, has_basic_means, expected):
    assert classify(typ, rodzaj, year, has_basic_means) == expected


def test_every_reject_reason_the_rule_can_return_is_listed():
    outcomes = {
        classify(typ, rodzaj, year, has_means)
        for typ, rodzaj in [
            ('liceum', YOUTH), ('liceum', ART), ('liceum', ADULT), ('technikum', YOUTH),
            ('branzowa', ART), ('liceum', None), ('technikum', ''),
        ]
        for year in (2023, 2024)
        for has_means in (True, False)
    }
    assert outcomes - {'liceum', 'technikum'} == set(REJECT_REASONS)


def test_an_art_liceum_is_a_liceum():
    # `nie dotyczy` licea are plastic-arts, music and ballet schools; 8th-graders
    # apply to them, so their results sit with youth licea.
    assert school_type('liceum', ART) == 'liceum'
    assert classify('liceum', ART, 2023, True) == 'liceum'


@pytest.mark.parametrize('rodzaj', ['', None, NAN])
def test_an_oke_pseudo_row_is_rejected_whatever_form_its_missing_rodzaj_takes(rodzaj):
    # '' in 2023-2025 and null in 2026 in the xlsx; pandas.read_excel gives NaN.
    assert school_type('liceum', rodzaj) == 'oke_pseudo_row'
    assert school_type('technikum', rodzaj) == 'oke_pseudo_row'


def test_a_missing_rodzaj_wins_over_branzowa():
    # 2024-2026 have OKE pseudo-rows typed `branzowa` with no rodzaj: rule 1 first.
    assert school_type('branzowa', None) == 'oke_pseudo_row'


def test_branzowa_is_rejected_whatever_its_rodzaj():
    assert school_type('branzowa', ART) == 'branzowa_ii'
    assert school_type('branzowa', YOUTH) == 'branzowa_ii'


@pytest.mark.parametrize(
    'typ, rodzaj',
    [
        ('technikum', ART),
        ('technikum', ADULT),
        ('szkola policealna', YOUTH),
        ('liceum', 'dla mlodziezy'),  # rodzaj keeps its diacritics; folding it is a bug
    ],
)
def test_an_unknown_category_raises_naming_it(typ, rodzaj):
    with pytest.raises(ValueError, match='unknown school category') as excinfo:
        school_type(typ, rodzaj)
    assert repr(rodzaj) in str(excinfo.value)
    with pytest.raises(ValueError, match='unknown school category'):
        classify(typ, rodzaj, 2025, True)


@pytest.mark.parametrize('has_basic_means', [True, False])
def test_a_2023_technikum_is_rejected_as_technikum_2023_even_without_basic_means(
    has_basic_means,
):
    # Rule 7 before rule 8: reversed, the 9 technika of 2023 (no basic means at
    # all) would land in missing_basic_subject.
    assert classify('technikum', YOUTH, 2023, has_basic_means) == 'technikum_2023'


def test_a_year_after_the_last_loaded_one_is_admitted_with_no_code_change():
    assert classify('liceum', YOUTH, 2027, True) == 'liceum'
    assert classify('technikum', YOUTH, 2027, True) == 'technikum'


def test_technika_start_in_2024():
    assert TECHNIKUM_FIRST_YEAR == 2024
    assert classify('technikum', YOUTH, TECHNIKUM_FIRST_YEAR, True) == 'technikum'


# --- art-school names ------------------------------------------------------------


def test_art_school_names_pass():
    assert_art_school_names([
        'LICEUM SZTUK PLASTYCZNYCH IM. JANA MATEJKI',
        'OGÓLNOKSZTAŁCĄCA SZKOŁA MUZYCZNA II STOPNIA',
        'OGÓLNOKSZTAŁCĄCA SZKOŁA BALETOWA',
        'Liceum Plastyczne',  # case does not matter
        'LICEUM ARTYSTYCZNE',
    ])


def test_a_name_that_is_not_an_art_school_raises_naming_it():
    with pytest.raises(ValueError, match='I LICEUM OGÓLNOKSZTAŁCĄCE'):
        assert_art_school_names(['LICEUM SZTUK PLASTYCZNYCH', 'I LICEUM OGÓLNOKSZTAŁCĄCE'])


def test_the_art_pattern_is_the_specs():
    assert ART_SCHOOL_RE.pattern == 'SZTUK|PLASTYCZN|MUZYCZN|BALET|ARTYST'


# --- populations (spec §5.2) -----------------------------------------------------


@pytest.mark.parametrize(
    'population, kind, year, expected',
    [
        ('liceum', 'liceum', 2023, True),
        ('liceum', 'liceum', 2026, True),
        ('liceum', 'technikum', 2025, False),
        ('technikum', 'technikum', 2024, True),
        ('technikum', 'technikum', 2023, False),
        ('technikum', 'liceum', 2025, False),
        ('all', 'liceum', 2023, False),  # `all` starts in 2024
        ('all', 'liceum', 2024, True),
        ('all', 'technikum', 2024, True),
        ('all', 'technikum', 2023, False),
    ],
)
def test_population_membership(population, kind, year, expected):
    assert in_population(population, kind, year) is expected


def test_an_unknown_population_raises():
    with pytest.raises(ValueError, match='unknown population'):
        in_population('branzowa', 'liceum', 2025)
    with pytest.raises(ValueError, match='unknown population'):
        export_subjects('licea')


def test_populations():
    assert POPULATIONS == ('liceum', 'technikum', 'all')


def test_liceum_and_all_export_every_mapped_subject():
    expected = ['polski', 'matematyka', 'angielski', 'composite_min', *EXTENDED_SUBJECTS]
    assert export_subjects('liceum') == expected
    assert export_subjects('all') == expected
    assert len(expected) == 15


def test_technikum_exports_everything_but_art_history():
    subjects = export_subjects('technikum')
    assert 'historia_sztuki_r' not in subjects
    assert subjects == [s for s in export_subjects('liceum') if s != 'historia_sztuki_r']
    assert len(subjects) == 14


@pytest.mark.parametrize('population', POPULATIONS)
def test_subject_groups_split_the_export_list_in_order(population):
    groups = subject_groups(population)
    assert groups['basic'] == ['polski', 'matematyka', 'angielski', 'composite_min']
    assert groups['basic'] + groups['extended'] == export_subjects(population)


def test_the_only_exclusion_is_art_history_in_technika():
    assert POPULATION_EXCLUSIONS == {'technikum': frozenset({'historia_sztuki_r'})}


# --- the subject key map (spec §4, §8.2) -----------------------------------------


def test_subject_keys():
    assert BASIC_KEYS == ['polski', 'matematyka', 'angielski']
    assert COMPOSITE == 'composite_min'
    assert list(EXTENDED_SUBJECTS) == [
        'angielski_r', 'matematyka_r', 'polski_r', 'biologia_r', 'chemia_r', 'fizyka_r',
        'geografia_r', 'historia_r', 'informatyka_r', 'historia_sztuki_r', 'niemiecki_r',
    ]
    assert SUBJECT_KEYS == BASIC_KEYS + list(EXTENDED_SUBJECTS)
    assert len(SUBJECT_KEYS) == 14


def test_only_exact_m_suffixed_names_are_mapped():
    for band, subject in [*BASIC_SUBJECTS.values(), *EXTENDED_SUBJECTS.values()]:
        assert subject.endswith(' (m)'), subject
    assert all(band == BASIC_BAND for band, _ in BASIC_SUBJECTS.values())
    assert all(band == EXTENDED_BAND for band, _ in EXTENDED_SUBJECTS.values())
    assert all(' poziom rozszerzony ' in s for _, s in EXTENDED_SUBJECTS.values())
    mapped = {s for _, s in [*BASIC_SUBJECTS.values(), *EXTENDED_SUBJECTS.values()]}
    assert 'matematyka poziom podstawowy' not in mapped  # the Lithuanian-maths exam
    assert len(mapped) == 14


@pytest.mark.parametrize(
    'raw, normalised',
    [
        # Raw header cells as they appear in the September 2023-2026 files.
        ('egzaminy obowiazkowe', BASIC_BAND),
        ('egzaminy dodatkowe', EXTENDED_BAND),
        ('.', WHOLE_EXAM[0]),
        ('dla całego egzaminu dojrzałości', WHOLE_EXAM[1]),
        ('* liczba zdających', MEASURE_N),
        ('średni wynik (%)', MEASURE_MEAN),
        ('mediana (%)', MEASURE_MEDIAN),
        ('* zdawalność (%)', MEASURE_PASS_RATE),
        ('język polski poziom podstawowy (M)', BASIC_SUBJECTS['polski'][1]),
        ('matematyka poziom podstawowy (M)', BASIC_SUBJECTS['matematyka'][1]),
        ('język angielski poziom podstawowy (M)', BASIC_SUBJECTS['angielski'][1]),
        ('język angielski poziom rozszerzony (M)', EXTENDED_SUBJECTS['angielski_r'][1]),
        ('matematyka poziom rozszerzony (M)', EXTENDED_SUBJECTS['matematyka_r'][1]),
        ('język polski poziom rozszerzony (M)', EXTENDED_SUBJECTS['polski_r'][1]),
        ('biologia poziom rozszerzony (M)', EXTENDED_SUBJECTS['biologia_r'][1]),
        ('chemia poziom rozszerzony (M)', EXTENDED_SUBJECTS['chemia_r'][1]),
        ('fizyka poziom rozszerzony (M)', EXTENDED_SUBJECTS['fizyka_r'][1]),
        ('geografia poziom rozszerzony (M)', EXTENDED_SUBJECTS['geografia_r'][1]),
        ('historia poziom rozszerzony (M)', EXTENDED_SUBJECTS['historia_r'][1]),
        ('informatyka poziom rozszerzony (M)', EXTENDED_SUBJECTS['informatyka_r'][1]),
        ('historia sztuki poziom rozszerzony (M)', EXTENDED_SUBJECTS['historia_sztuki_r'][1]),
        ('język niemiecki poziom rozszerzony (M)', EXTENDED_SUBJECTS['niemiecki_r'][1]),
    ],
)
def test_the_literals_are_the_normalised_source_labels(raw, normalised):
    assert normalize_label(raw) == normalised


# --- unsuffixed subjects (spec §2.3) ---------------------------------------------

COLUMNS_2023 = [
    ('meta', '', 'nazwa szkoly'),
    ('.', 'dla calego egzaminu dojrzalosci', 'zdawalnosc (%)'),
    (BASIC_BAND, 'matematyka poziom podstawowy (m)', MEASURE_N),
    (BASIC_BAND, 'matematyka poziom podstawowy', MEASURE_N),
    (BASIC_BAND, 'matematyka poziom podstawowy', MEASURE_MEAN),
    (EXTENDED_BAND, 'matematyka poziom rozszerzony', MEASURE_N),
    (EXTENDED_BAND, 'matematyka poziom rozszerzony', MEASURE_MEAN),
    (EXTENDED_BAND, 'wiedza o spoleczenstwie poziom rozszerzony (m19-21)', MEASURE_MEAN),
    (EXTENDED_BAND, 'wiedza o spoleczenstwie poziom rozszerzony (m22)', MEASURE_MEAN),
    ('zestawy zadan w jezyku obcym', 'matematyka poziom rozszerzony (m)', MEASURE_MEAN),
]


def test_the_lithuanian_maths_exam_is_found_in_both_bands():
    assert unsuffixed_subjects(COLUMNS_2023) == {
        (BASIC_BAND, 'matematyka poziom podstawowy'),
        (EXTENDED_BAND, 'matematyka poziom rozszerzony'),
    }


def test_any_parenthesised_suffix_counts_as_a_suffix():
    columns = [(EXTENDED_BAND, 'wiedza o spoleczenstwie poziom rozszerzony (m22)', MEASURE_N)]
    assert unsuffixed_subjects(columns) == set()


def test_an_unsuffixed_subject_in_the_foreign_language_band_is_found():
    columns = [('zestawy zadan w jezyku obcym', 'fizyka poziom rozszerzony', MEASURE_N)]
    assert unsuffixed_subjects(columns) == {
        ('zestawy zadan w jezyku obcym', 'fizyka poziom rozszerzony'),
    }


def test_unsuffixed_subjects_with_takers_but_no_mean_pass():
    # 2023: basic n = 4 and extended n = 1 in Lithuanian, never a published mean.
    published = dict.fromkeys(COLUMNS_2023, 0)
    published[(BASIC_BAND, 'matematyka poziom podstawowy', MEASURE_N)] = 1
    published[(EXTENDED_BAND, 'matematyka poziom rozszerzony', MEASURE_N)] = 1
    assert_unsuffixed_unpublished(published)


def test_an_unsuffixed_subject_with_a_published_mean_raises_naming_it():
    published = dict.fromkeys(COLUMNS_2023, 0)
    published[(EXTENDED_BAND, 'matematyka poziom rozszerzony', MEASURE_MEAN)] = 1
    with pytest.raises(ValueError, match='matematyka poziom rozszerzony'):
        assert_unsuffixed_unpublished(published)


def test_an_other_suffixed_subject_with_a_mean_stays_unmapped_without_raising():
    # 2026 civics: (m22) has a mean at 998 licea; out of scope, not an error.
    published = dict.fromkeys(COLUMNS_2023, 0)
    published[(EXTENDED_BAND, 'wiedza o spoleczenstwie poziom rozszerzony (m22)',
               MEASURE_MEAN)] = 998
    assert_unsuffixed_unpublished(published)


# --- the coverage rule (spec §4.2) -----------------------------------------------

YEARS = [2023, 2024, 2025, 2026]


def test_coverage_needs_the_minimum_in_every_year():
    counts = {
        'at threshold': dict.fromkeys(YEARS, COVERAGE_MIN_LICEA),
        'one weak year': {2023: 500, 2024: 500, 2025: COVERAGE_MIN_LICEA - 1, 2026: 500},
        'missing a year': {2023: 500, 2024: 500, 2025: 500},
    }
    assert passing_coverage(counts, YEARS) == {'at threshold'}
    assert COVERAGE_MIN_LICEA == 100


def test_coverage_looks_only_at_the_years_given():
    counts = {'new in 2024': {2024: 300, 2025: 300, 2026: 300}}
    assert passing_coverage(counts, [2024, 2025, 2026]) == {'new in 2024'}
    assert passing_coverage(counts, YEARS) == set()


def test_coverage_matching_the_key_map_passes():
    assert_coverage_matches(set(EXTENDED_SOURCE))


def test_an_unmapped_subject_that_passes_coverage_raises_naming_it():
    extra = 'jezyk francuski poziom rozszerzony (m)'
    with pytest.raises(ValueError, match=r'passing but unmapped \[.jezyk francuski'):
        assert_coverage_matches(EXTENDED_SOURCE | {extra})


def test_a_mapped_subject_that_fails_coverage_raises_naming_it():
    # German sits 4 above the rule in 2025; a weak year must fail loudly, not
    # make the subject vanish.
    german = EXTENDED_SUBJECTS['niemiecki_r'][1]
    with pytest.raises(ValueError, match=r'mapped but failing \[.jezyk niemiecki'):
        assert_coverage_matches(EXTENDED_SOURCE - {german})


def test_a_coverage_mismatch_names_both_differences():
    extra = 'filozofia poziom rozszerzony (m)'
    german = EXTENDED_SUBJECTS['niemiecki_r'][1]
    with pytest.raises(ValueError) as excinfo:
        assert_coverage_matches((EXTENDED_SOURCE - {german}) | {extra})
    assert extra in str(excinfo.value)
    assert german in str(excinfo.value)


# --- the per-population exclusions (spec §4.2) -----------------------------------


def test_exclusions_matching_the_short_subjects_pass():
    assert_population_exclusions('technikum', {'historia_sztuki_r'})
    assert_population_exclusions('liceum', set())
    assert_population_exclusions('all', set())


def test_a_short_subject_that_is_not_excluded_raises_naming_it():
    with pytest.raises(ValueError, match=r"short but not excluded \['niemiecki_r'\]"):
        assert_population_exclusions('liceum', {'niemiecki_r'})


def test_an_excluded_subject_that_is_no_longer_short_raises_naming_it():
    # Art history flips into technikum/ once one voivodeship reaches 5 scored
    # technika: a human decides, the rule does not.
    with pytest.raises(ValueError, match=r"excluded but not short \['historia_sztuki_r'\]"):
        assert_population_exclusions('technikum', set())


def test_an_exclusion_mismatch_names_both_differences():
    with pytest.raises(ValueError) as excinfo:
        assert_population_exclusions('technikum', {'niemiecki_r'})
    assert 'niemiecki_r' in str(excinfo.value)
    assert 'historia_sztuki_r' in str(excinfo.value)


def test_exclusions_for_an_unknown_population_raise():
    with pytest.raises(ValueError, match='unknown population'):
        assert_population_exclusions('technika', set())
