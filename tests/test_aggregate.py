"""Aggregating schools into a region, and deciding what may be published."""

import pytest

from school_quality.aggregate import (
    WEIGHTED_METRICS,
    aggregate_region,
    rank_and_percentile,
    region_percentile_publishable,
    region_score_publishable,
    sibling_percentiles,
    weighting_for,
)

# ── which weighting a metric gets ────────────────────────────────────────────

def test_difference_metrics_are_student_weighted():
    # A 300-pupil school should move a gmina more than a 30-pupil one, exactly
    # as the metric already weights across years.
    assert WEIGHTED_METRICS == frozenset({'diff_mean', 'unit_norm_diff_mean'})
    assert weighting_for('unit_norm_diff_mean') == 'weighted'


def test_raw_metrics_are_arithmetic():
    assert weighting_for('mean') == 'arithmetic'
    assert weighting_for('median') == 'arithmetic'


def test_an_unknown_metric_is_rejected():
    with pytest.raises(KeyError):
        weighting_for('mode')


# ── the aggregate itself ─────────────────────────────────────────────────────

def test_weighted_aggregate_follows_the_students():
    # 10 pupils at 0.0 and 90 at 1.0 -> 0.9, not the unweighted 0.5.
    assert aggregate_region([0.0, 1.0], [10, 90], 'weighted') == pytest.approx(0.9)


def test_arithmetic_aggregate_ignores_the_weights():
    assert aggregate_region([0.0, 1.0], [10, 90], 'arithmetic') == pytest.approx(0.5)


def test_the_two_weightings_genuinely_differ():
    # Guards against an implementation that ignores `statistic`.
    scores, weights = [0.0, 1.0], [10, 90]
    assert aggregate_region(scores, weights, 'weighted') != aggregate_region(
        scores, weights, 'arithmetic')


def test_schools_with_no_score_are_skipped_not_counted_as_zero():
    assert aggregate_region([1.0, None, 3.0], [1, 1, 1], 'arithmetic') == pytest.approx(2.0)


def test_an_empty_region_has_no_aggregate():
    assert aggregate_region([], [], 'arithmetic') is None
    assert aggregate_region([None, None], [1, 1], 'weighted') is None


def test_zero_total_weight_falls_back_rather_than_dividing_by_zero():
    assert aggregate_region([1.0, 3.0], [0, 0], 'weighted') == pytest.approx(2.0)


# ── rank and percentile, in the school path's convention ─────────────────────

def test_rank_one_is_the_highest_score():
    ranks, pcts = rank_and_percentile([10.0, 30.0, 20.0])
    assert ranks == [3, 1, 2]
    assert pcts == [33.3, 100.0, 66.7]


def test_ties_take_the_minimum_rank_and_the_average_percentile():
    # Two schools tied at the top both rank 1; the next is 3, not 2. This is
    # stats.rankdata(method='min') on the negated scores, matching cell #33.
    ranks, pcts = rank_and_percentile([5.0, 5.0, 1.0])
    assert ranks == [1, 1, 3]
    # ascending average ranks are 2.5, 2.5, 1 -> /3*100
    assert pcts == [83.3, 83.3, 33.3]


def test_the_best_region_is_the_hundredth_percentile():
    # The old inline "count of peers strictly below me" gave (n-1)/n here -
    # 66.7 rather than 100.0 - which is why this convention is pinned.
    _, pcts = rank_and_percentile([1.0, 2.0, 3.0])
    assert pcts[2] == 100.0


def test_regions_without_a_score_rank_as_none_and_do_not_count_towards_n():
    ranks, pcts = rank_and_percentile([10.0, None, 20.0])
    assert ranks == [2, None, 1]
    assert pcts == [50.0, None, 100.0]


def test_a_level_with_no_scores_at_all_ranks_nothing():
    assert rank_and_percentile([None, None]) == ([None, None], [None, None])


# ── what may be published ────────────────────────────────────────────────────

def test_a_score_needs_a_big_enough_PARENT():
    # The gate is the population the region is compared WITHIN - its parent -
    # not the region's own schools.
    assert region_score_publishable('unit_norm_diff_mean', 5, 10) is True
    assert region_score_publishable('unit_norm_diff_mean', 4, 10) is False


def test_an_only_child_never_publishes_a_difference_score():
    # 66 powiats hold exactly one gmina. Comparing that gmina against its parent
    # compares it against itself - and because the aggregate is weighted while
    # the reference is not, it does not even cancel to zero.
    assert region_score_publishable('unit_norm_diff_mean', 377, 1) is False


def test_raw_metric_SCORES_are_never_suppressed():
    # `mean` and `median` have no reference population, so no region is too
    # small for them. Gating them would blank all 66 city gminas - Warszawa
    # included - for a rule that does not apply to them.
    assert region_score_publishable('mean', 1, 1) is True
    assert region_score_publishable('median', 0, 1) is True


def test_the_percentile_gate_applies_to_raw_metrics_too():
    # The asymmetry that matters: a percentile is a position among siblings, so
    # an only child has none even for `mean`. Without this it publishes the
    # 100th percentile of itself.
    assert region_percentile_publishable(1) is False
    assert region_percentile_publishable(3) is False


def test_a_percentile_needs_enough_SIBLINGS():
    assert region_percentile_publishable(8) is True
    assert region_percentile_publishable(7) is False


def test_a_percentile_ignores_the_school_count_entirely():
    # A gmina of 300 schools among 3 siblings still has a meaningless percentile.
    assert region_percentile_publishable(3) is False


def test_an_only_child_never_publishes_a_percentile():
    assert region_percentile_publishable(1) is False


def test_a_percentile_counts_only_the_siblings_that_have_a_score():
    # Nine gminas in the geometry, but two hold no scored school. The ranking
    # runs over the seven that are left, so seven is the population the gate
    # has to see - counting polygons would publish a 1/7-grained percentile.
    scores = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, None, float('nan')]
    assert sibling_percentiles(scores) == [None] * 9


def test_eight_scored_siblings_are_enough_and_the_unscored_one_stays_blank():
    # The real edge today: a powiat of nine gminas, one of them empty.
    scores = [0.1, 0.2, 0.3, 0.4, None, 0.5, 0.6, 0.7, 0.8]
    assert sibling_percentiles(scores) == rank_and_percentile(scores)[1]
    assert sibling_percentiles(scores)[4] is None
