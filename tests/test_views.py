"""Per-school views of a multi-year score, and ranking within one view."""

import math

import numpy as np
import pytest

from school_quality.views import (
    AGGREGATION_BY_METRIC,
    aggregate_years,
    arithmetic_mean,
    cell_payload,
    composite_min,
    composite_views,
    rank_view,
    school_views,
    view_param_key,
    weighted_mean,
)


def test_weighted_mean_ignores_nan_values_and_non_positive_weights():
    assert weighted_mean([1.0, 2.0, 3.0], [1, 1, 2]) == pytest.approx(2.25)
    assert weighted_mean([1.0, float('nan'), 3.0], [1, 5, 1]) == pytest.approx(2.0)
    assert weighted_mean([1.0, 9.0], [2, 0]) == pytest.approx(1.0)
    assert math.isnan(weighted_mean([float('nan')], [3]))


def test_arithmetic_mean_ignores_nan():
    assert arithmetic_mean([1.0, float('nan'), 3.0]) == pytest.approx(2.0)
    assert math.isnan(arithmetic_mean([float('nan')]))


def test_raw_metrics_average_years_equally_and_difference_metrics_weight_by_cohort():
    assert AGGREGATION_BY_METRIC['mean'] is arithmetic_mean
    assert AGGREGATION_BY_METRIC['median'] is arithmetic_mean
    assert AGGREGATION_BY_METRIC['diff_mean'] is weighted_mean
    assert AGGREGATION_BY_METRIC['unit_norm_diff_mean'] is weighted_mean


YEAR_MAP = {2023: (10, 50.0, 7), 2024: (30, 60.0, 5), 2025: (20, 70.0, 9)}


def test_aggregate_years_weights_scores_and_takes_median_cohort_and_min_reference():
    score, n_students, reference_n = aggregate_years('diff_mean', YEAR_MAP, [2023, 2024])
    assert score == pytest.approx(57.5)       # (10*50 + 30*60) / 40
    assert n_students == 20                   # median of 10 and 30
    assert reference_n == 5                   # the smallest reference the view uses
    score, n_students, reference_n = aggregate_years('mean', YEAR_MAP, [2023, 2024, 2025])
    assert score == pytest.approx(60.0)
    assert (n_students, reference_n) == (20, 5)


def test_aggregate_years_over_years_the_school_lacks_is_empty():
    score, n_students, reference_n = aggregate_years('mean', YEAR_MAP, [2022])
    assert math.isnan(score)
    assert (n_students, reference_n) == (0, 0)


def test_one_year_gives_base_and_single_year_only():
    assert school_views([2024]) == [('base', None, [2024]), ('single_year', 2024, [2024])]


def test_two_years_add_leave_one_out_but_no_last_k():
    assert school_views([2024, 2025]) == [
        ('base', None, [2024, 2025]),
        ('loo', 2024, [2025]), ('loo', 2025, [2024]),
        ('single_year', 2024, [2024]), ('single_year', 2025, [2025]),
    ]


def test_four_years_in_e8_order():
    views = school_views([2023, 2024, 2025, 2026])
    assert [(kind, param) for kind, param, _ in views] == [
        ('base', None),
        ('loo', 2023), ('loo', 2024), ('loo', 2025), ('loo', 2026),
        ('single_year', 2023), ('single_year', 2024), ('single_year', 2025),
        ('single_year', 2026),
        ('last_k', 2), ('last_k', 3),
    ]
    assert views[-2][2] == [2025, 2026]
    assert views[-1][2] == [2024, 2025, 2026]


def test_a_subject_with_a_gap_uses_only_its_own_years():
    # Review focus 3: an extended subject sat in 2023 and 2025-2026 but not 2024.
    views = {(kind, param): years for kind, param, years in school_views([2023, 2025, 2026])}
    assert views[('base', None)] == [2023, 2025, 2026]
    assert views[('loo', 2023)] == [2025, 2026]
    assert views[('last_k', 2)] == [2025, 2026]
    assert ('single_year', 2024) not in views


SCORES = {'polski': {1: 0.1, 2: -0.2, 3: 0.0},
          'matematyka': {1: -0.1, 2: -0.2},
          'angielski': {1: 0.3, 2: 0.5}}
COHORTS = {'polski': {1: 10, 2: 20, 3: 5},
           'matematyka': {1: 11, 2: 21},
           'angielski': {1: 12, 2: 22}}


def test_composite_min_takes_the_weakest_subject_and_its_cohort():
    score, n = composite_min(SCORES, COHORTS, ['polski', 'matematyka', 'angielski'])
    assert score[1] == pytest.approx(-0.1)
    assert n[1] == 11


def test_composite_min_breaks_a_tie_by_subject_order():
    score, n = composite_min(SCORES, COHORTS, ['polski', 'matematyka', 'angielski'])
    assert score[2] == pytest.approx(-0.2)
    assert n[2] == 20                         # polski comes first in the order given
    _, n_swapped = composite_min(SCORES, COHORTS, ['matematyka', 'polski', 'angielski'])
    assert n_swapped[2] == 21


def test_composite_min_needs_every_subject():
    score, n = composite_min(SCORES, COHORTS, ['polski', 'matematyka', 'angielski'])
    assert set(score) == {1, 2}               # school 3 lacks maths and English
    assert set(n) == {1, 2}


def test_composite_views_follow_the_order_the_subjects_views_were_stored_in():
    """Spec §8.3 and §9.2: never re-sorted and never through a set, whose order of
    ('loo', 2021)-style keys changes from one process to the next."""
    stored = [('single_year', 2025), ('loo', 2024), ('base', None), ('last_k', 2)]
    view_results, view_n = {}, {}
    for kind, param in stored:
        for subject in ('polski', 'matematyka', 'angielski'):
            view_results[('national', 'mean', subject, kind, param)] = {1: 50.0, 2: 40.0}
            view_n[('national', 'mean', subject, kind, param)] = {1: 10, 2: 20}
    view_results[('voivodeship', 'mean', 'polski', 'loo', 2023)] = {1: 0.0}  # another level
    view_n[('voivodeship', 'mean', 'polski', 'loo', 2023)] = {1: 10}
    results, n = composite_views(view_results, view_n, 'national', 'mean',
                                 ['polski', 'matematyka', 'angielski'])
    assert [key[3:] for key in results] == stored
    assert all(key[:3] == ('national', 'mean', 'composite_min') for key in results)
    assert list(n) == list(results)


def test_a_composite_view_needs_a_school_with_every_subject():
    view_results = {('national', 'mean', 'polski', 'base', None): {1: 50.0},
                    ('national', 'mean', 'matematyka', 'base', None): {2: 40.0},
                    ('national', 'mean', 'angielski', 'base', None): {1: 60.0, 2: 70.0}}
    view_n = {key: dict.fromkeys(scores, 10) for key, scores in view_results.items()}
    assert composite_views(view_results, view_n, 'national', 'mean',
                           ['polski', 'matematyka', 'angielski']) == ({}, {})


EIGHT = {10: 0.5, 20: 0.1, 30: 0.5, 40: -0.3, 50: 0.2, 60: 0.0, 70: 0.4, 80: 0.3}


def test_rank_view_ranks_descending_with_min_ties_and_average_percentiles():
    rows = rank_view(EIGHT)
    assert [r[0] for r in rows] == list(EIGHT)            # insertion order kept
    by_rspo = {rspo: (rank, pct, n) for rspo, _, rank, pct, n in rows}
    assert by_rspo[10] == (1, pytest.approx(93.75), 8)
    assert by_rspo[30] == (1, pytest.approx(93.75), 8)
    assert by_rspo[70] == (3, pytest.approx(75.0), 8)
    assert by_rspo[40] == (8, pytest.approx(12.5), 8)
    assert all(isinstance(rank, int) for _, _, rank, _, _ in rows)


def test_rank_view_withholds_percentiles_below_eight_schools():
    # Review focus 4: a view ranking 7 schools publishes ranks but no percentile.
    seven = {r: s for r, s in EIGHT.items() if r != 80}
    rows = rank_view(seven)
    assert all(pct is None for _, _, _, pct, _ in rows)
    assert all(n == 7 for *_, n in rows)
    assert {rspo: rank for rspo, _, rank, _, _ in rows}[40] == 7


def test_rank_view_of_an_empty_view_is_empty():
    assert rank_view({}) == []


@pytest.mark.parametrize(('kind', 'param', 'key'), [
    ('base', None, None),
    ('loo', 2024, '2024'),
    ('last_k', 2.0, '2'),
    ('single_year', float('nan'), ''),
])
def test_view_param_key(kind, param, key):
    assert view_param_key(kind, param) == key


def test_cell_payload_rounds_and_withholds_a_missing_percentile():
    assert cell_payload(0.123456, 3, 45.678) == {'score': 0.1235, 'rank': 3, 'pct': 45.7}
    assert cell_payload(0.5, 1, None) == {'score': 0.5, 'rank': 1, 'pct': None}
    assert cell_payload(0.5, 1, float('nan'))['pct'] is None


def test_cell_payload_returns_plain_python_numbers():
    cell = cell_payload(np.float64(0.5), np.int64(2), np.float64(50.0))
    assert cell == {'score': 0.5, 'rank': 2, 'pct': 50.0}
    assert type(cell['rank']) is int
    assert type(cell['score']) is float
