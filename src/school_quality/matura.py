"""EM2023 (new-formula matura) rules: which rows are in the population, and which subjects.

This module is the single owner of the matura pipeline's subject list (spec §4.2,
§8.2): the source-subject -> key maps below, keyed on the full normalised source
name with its `(m)` suffix, and POPULATION_EXCLUSIONS beside them. The notebook
asserts that the >= 100-licea coverage rule (section 2) and the >= 2-base-score
rule (section 6) agree with these literals in both directions; neither rule ever
edits them. A breach is for a human to decide.

Header keys follow `header.normalize_label`: band, subject and measure are
lowercase ASCII. The classifier takes raw cell values instead: `typ placówki` is
ASCII in the source (`liceum`, `technikum`, `branzowa`), while `rodzaj placówki`
keeps its diacritics (`dla młodzieży`, `dla dorosłych`, `nie dotyczy`). Callers
strip whitespace from both and fold nothing else.
"""

import math
import re
from collections.abc import Iterable, Mapping

# New-system (five-year) technika first sat EM2023 in 2024. The same year starts
# the `all` population: in 2023 a combined reference would be the liceum one.
TECHNIKUM_FIRST_YEAR = 2024

BASIC_BAND = 'egzaminy obowiazkowe'
EXTENDED_BAND = 'egzaminy dodatkowe'
# The whole-exam block: band '.', one subject, read by measure name.
WHOLE_EXAM = ('.', 'dla calego egzaminu dojrzalosci')

MEASURE_N = 'liczba zdajacych'
MEASURE_MEAN = 'sredni wynik (%)'
MEASURE_MEDIAN = 'mediana (%)'
MEASURE_PASS_RATE = 'zdawalnosc (%)'

# Only names ending in exactly `(m)` are mapped. An unsuffixed namesake is a
# different exam (maths sat in Lithuanian); `(m19-21)` / `(m22)` are 2026 civics.
BASIC_SUBJECTS: dict[str, tuple[str, str]] = {
    'polski': (BASIC_BAND, 'jezyk polski poziom podstawowy (m)'),
    'matematyka': (BASIC_BAND, 'matematyka poziom podstawowy (m)'),
    'angielski': (BASIC_BAND, 'jezyk angielski poziom podstawowy (m)'),
}
EXTENDED_SUBJECTS: dict[str, tuple[str, str]] = {
    'angielski_r': (EXTENDED_BAND, 'jezyk angielski poziom rozszerzony (m)'),
    'matematyka_r': (EXTENDED_BAND, 'matematyka poziom rozszerzony (m)'),
    'polski_r': (EXTENDED_BAND, 'jezyk polski poziom rozszerzony (m)'),
    'biologia_r': (EXTENDED_BAND, 'biologia poziom rozszerzony (m)'),
    'chemia_r': (EXTENDED_BAND, 'chemia poziom rozszerzony (m)'),
    'fizyka_r': (EXTENDED_BAND, 'fizyka poziom rozszerzony (m)'),
    'geografia_r': (EXTENDED_BAND, 'geografia poziom rozszerzony (m)'),
    'historia_r': (EXTENDED_BAND, 'historia poziom rozszerzony (m)'),
    'informatyka_r': (EXTENDED_BAND, 'informatyka poziom rozszerzony (m)'),
    'historia_sztuki_r': (EXTENDED_BAND, 'historia sztuki poziom rozszerzony (m)'),
    'niemiecki_r': (EXTENDED_BAND, 'jezyk niemiecki poziom rozszerzony (m)'),
}
SUBJECT_KEYS = list(BASIC_SUBJECTS) + list(EXTENDED_SUBJECTS)
COMPOSITE = 'composite_min'
BASIC_KEYS = list(BASIC_SUBJECTS)

POPULATIONS = ('liceum', 'technikum', 'all')
# No technikum voivodeship-year reaches 5 scored schools in art history, so it has
# no score at the voivodeship reference (spec §4.2). Asserted in notebook section 6.
POPULATION_EXCLUSIONS: dict[str, frozenset[str]] = {
    'technikum': frozenset({'historia_sztuki_r'}),
}

# Every `nie dotyczy` liceum is an art school: plastic arts, music or ballet.
ART_SCHOOL_RE = re.compile(r'SZTUK|PLASTYCZN|MUZYCZN|BALET|ARTYST', re.IGNORECASE)
COVERAGE_MIN_LICEA = 100

REJECT_REASONS = (
    'oke_pseudo_row',
    'branzowa_ii',
    'adult_liceum',
    'technikum_2023',
    'missing_basic_subject',
)

_YOUTH = 'dla młodzieży'
_ADULT = 'dla dorosłych'
_NOT_APPLICABLE = 'nie dotyczy'
_TYPES = ('liceum', 'technikum')


def _is_missing(value) -> bool:
    """None, NaN or an empty string: the three forms an OKE pseudo-row's `rodzaj` takes.

    The cell is an empty string in 2023-2025 and null in 2026; `pandas.read_excel`
    turns both into NaN.
    """
    if value is None:
        return True
    if isinstance(value, float):
        return math.isnan(value)
    return value == ''


def _check_population(population: str) -> None:
    if population not in POPULATIONS:
        raise ValueError(f'unknown population {population!r}; expected one of {POPULATIONS}')


def school_type(typ: str, rodzaj: str | None) -> str:
    """Rules 1-6 of spec §3.1, first match wins: a type or a reject reason.

    An unknown (typ, rodzaj) pair raises: it is a source change to look at, not a
    row to drop.
    """
    if _is_missing(rodzaj):
        return 'oke_pseudo_row'
    if typ == 'branzowa':
        return 'branzowa_ii'
    if typ == 'liceum' and rodzaj == _ADULT:
        return 'adult_liceum'
    if typ == 'liceum' and rodzaj in (_YOUTH, _NOT_APPLICABLE):
        return 'liceum'
    if typ == 'technikum' and rodzaj == _YOUTH:
        return 'technikum'
    raise ValueError(f'unknown school category: typ={typ!r}, rodzaj={rodzaj!r}')


def classify(typ: str, rodzaj: str | None, year: int, has_basic_means: bool) -> str:
    """'liceum', 'technikum' or one of REJECT_REASONS (spec §3.1, rules 1-8).

    Rule 7 runs before rule 8: reversed, the 9 technika of 2023 (no basic means)
    would land in `missing_basic_subject`.
    """
    kind = school_type(typ, rodzaj)
    if kind not in _TYPES:
        return kind
    if kind == 'technikum' and year < TECHNIKUM_FIRST_YEAR:
        return 'technikum_2023'
    if not has_basic_means:
        return 'missing_basic_subject'
    return kind


def assert_art_school_names(names: Iterable[str]) -> None:
    """Every `nie dotyczy` liceum name matches ART_SCHOOL_RE, so nothing else slips
    in under that value."""
    offenders = [name for name in names if not ART_SCHOOL_RE.search(name)]
    if offenders:
        raise ValueError(
            f'{len(offenders)} "nie dotyczy" licea have no art-school name '
            f'({ART_SCHOOL_RE.pattern}), e.g. {offenders[:5]}'
        )


def unsuffixed_subjects(columns: Iterable[tuple[str, str, str]]) -> set[tuple[str, str]]:
    """(band, subject) pairs in the exam bands whose subject has no parenthesised suffix.

    Such a subject is a different exam (2023: maths sat in Lithuanian at one
    liceum), never mapped. The `.` band and the meta columns are outside the rule.
    """
    return {
        (band, subject)
        for band, subject, _ in columns
        if band.startswith(('egzaminy', 'zestawy')) and not subject.endswith(')')
    }


def assert_unsuffixed_unpublished(published: Mapping[tuple[str, str, str], int]) -> None:
    """No unsuffixed subject carries a published mean (spec §2.3).

    `published` maps every column key of one file to its number of non-null cells
    (`frame.notna().sum().to_dict()`). Only the MEASURE_MEAN column of an
    unsuffixed subject is checked: its count of takers may be present (n < 5). A
    subject with any other suffix, e.g. `(m22)`, is unmapped without assertion.
    """
    unsuffixed = unsuffixed_subjects(published)
    offenders = sorted(
        (band, subject, count)
        for (band, subject, measure), count in published.items()
        if (band, subject) in unsuffixed and measure == MEASURE_MEAN and count > 0
    )
    if offenders:
        raise ValueError(f'unsuffixed subjects with a published mean: {offenders}')


def in_population(population: str, school_type: str, year: int) -> bool:
    """Whether a population row of `school_type` in `year` belongs to `population`."""
    if population == 'liceum':
        return school_type == 'liceum'
    if population == 'technikum':
        return school_type == 'technikum' and year >= TECHNIKUM_FIRST_YEAR
    if population == 'all':
        return school_type in _TYPES and year >= TECHNIKUM_FIRST_YEAR
    raise ValueError(f'unknown population {population!r}; expected one of {POPULATIONS}')


def _extended_keys(population: str) -> list[str]:
    _check_population(population)
    excluded = POPULATION_EXCLUSIONS.get(population, frozenset())
    return [s for s in EXTENDED_SUBJECTS if s not in excluded]


def export_subjects(population: str) -> list[str]:
    """The population's published subject keys, in export order."""
    return BASIC_KEYS + [COMPOSITE] + _extended_keys(population)


def subject_groups(population: str) -> dict[str, list[str]]:
    """`scale.json`'s `subject_groups`: basic + extended == export_subjects, in order."""
    return {'basic': BASIC_KEYS + [COMPOSITE], 'extended': _extended_keys(population)}


def passing_coverage(licea_counts: dict[str, dict[int, int]], years: Iterable[int]) -> set[str]:
    """Source subjects with a published mean at >= COVERAGE_MIN_LICEA population licea in
    every one of `years`. A year missing from a subject's counts counts as 0."""
    years = list(years)
    return {
        subject
        for subject, per_year in licea_counts.items()
        if all(per_year.get(year, 0) >= COVERAGE_MIN_LICEA for year in years)
    }


def assert_coverage_matches(passing_source_subjects: set[str]) -> None:
    """The coverage rule and EXTENDED_SUBJECTS agree in both directions."""
    mapped = {subject for _, subject in EXTENDED_SUBJECTS.values()}
    unmapped = sorted(set(passing_source_subjects) - mapped)
    failing = sorted(mapped - set(passing_source_subjects))
    if unmapped or failing:
        raise ValueError(
            f'the >= {COVERAGE_MIN_LICEA}-licea coverage rule disagrees with '
            f'EXTENDED_SUBJECTS: passing but unmapped {unmapped}; mapped but failing '
            f'{failing}. Decide by hand; the rule never edits the key map.'
        )


def assert_population_exclusions(population: str, short_subjects: set[str]) -> None:
    """The subjects with a scale entry below 2 base scores are exactly the population's
    POPULATION_EXCLUSIONS."""
    _check_population(population)
    expected = POPULATION_EXCLUSIONS.get(population, frozenset())
    unexpected = sorted(set(short_subjects) - expected)
    no_longer_short = sorted(expected - set(short_subjects))
    if unexpected or no_longer_short:
        raise ValueError(
            f'{population}: subjects with a scale entry below 2 base scores differ from '
            f'POPULATION_EXCLUSIONS: short but not excluded {unexpected}; excluded but '
            f'not short {no_longer_short}. Decide by hand.'
        )
