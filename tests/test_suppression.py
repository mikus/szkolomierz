"""Rules that stop small regions producing confident-looking nonsense."""

import numpy as np
import pytest

from school_quality.suppression import (
    MIN_PERCENTILE_N,
    MIN_REFERENCE_N,
    percentile_is_meaningful,
    rank_label,
    reference_is_usable,
    suppress_diff_score,
    suppress_percentile,
)


def test_thresholds_match_the_spec():
    assert MIN_REFERENCE_N == 5
    assert MIN_PERCENTILE_N == 8


@pytest.mark.parametrize('n, usable', [(1, False), (4, False), (5, True), (50, True)])
def test_a_reference_needs_five_schools(n, usable):
    assert reference_is_usable(n) is usable


def test_a_reference_check_returns_a_plain_bool_for_a_numpy_integer():
    # A pandas group size (e.g. from .size()) is a numpy integer. `n >=
    # MIN_REFERENCE_N` on one yields np.bool_, which fails `is True/False` -
    # exactly what the test above asserts. Coerce with bool() so this predicate's
    # identity contract holds regardless of the caller's integer type.
    assert reference_is_usable(np.int64(5)) is True
    assert percentile_is_meaningful(np.int64(8)) is True


def test_a_single_school_region_cannot_provide_a_reference():
    # The school IS the reference, so its difference from it is zero by
    # construction - not "average", but undefined wearing average's clothes.
    assert reference_is_usable(1) is False


@pytest.mark.parametrize('n, meaningful', [(1, False), (7, False), (8, True), (100, True)])
def test_a_percentile_needs_eight_schools(n, meaningful):
    assert percentile_is_meaningful(n) is meaningful


def test_a_diff_score_is_dropped_in_a_region_too_small_to_compare_within():
    assert suppress_diff_score(0.0, n=1) is None
    assert suppress_diff_score(0.12, n=4) is None


def test_a_diff_score_survives_once_the_region_is_big_enough():
    assert suppress_diff_score(0.12, n=5) == 0.12


def test_a_percentile_is_dropped_below_the_threshold():
    assert suppress_percentile(50.0, n=3) is None


def test_a_percentile_survives_at_the_threshold():
    assert suppress_percentile(64.9, n=8) == 64.9


def test_a_missing_score_stays_missing_rather_than_becoming_a_number():
    assert suppress_diff_score(None, n=100) is None
    assert suppress_percentile(None, n=100) is None


def test_nan_is_treated_as_missing_like_none():
    # The real call site (the notebook's export loop) holds numpy floats, not
    # None. A bare NaN reaching json.dumps writes an invalid-JSON `NaN` token
    # that the map app's fetch(...).json() rejects, so NaN must suppress exactly
    # like None does.
    assert suppress_percentile(float('nan'), n=100) is None
    assert suppress_diff_score(float('nan'), n=100) is None


def test_rank_label_reads_as_a_plain_fact():
    assert rank_label(2, 3) == '2 of 3'


def test_rank_label_is_what_a_small_region_shows_instead_of_a_percentile():
    n = MIN_PERCENTILE_N - 1
    assert suppress_percentile(50.0, n) is None
    assert rank_label(1, n) == f'1 of {n}'
