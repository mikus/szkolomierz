"""A school's views of a multi-year score, and ranking within one view.

Shared by both export notebooks (matura spec §8.3), extracted from the E8
notebook's export cells. A view is a set of the school's OWN years: base (all of
them), loo (all but one, only with >= 2 years), single_year, and last_k (the most
recent k, k = 2..n-1). Building views from the school's own years avoids
meaningless folds such as "exclude 2021" for a school with no 2021 data.

The orders here are load-bearing: the E8 shards are written in the order these
functions produce, and their bytes must not change.
"""

import math

import numpy as np
import pandas as pd
from scipy import stats

from school_quality.suppression import MIN_PERCENTILE_N


def weighted_mean(values, weights):
    values = np.asarray(values)
    weights = np.asarray(weights)
    valid = ~(np.isnan(values) | np.isnan(weights)) & (weights > 0)
    if not valid.any() or weights[valid].sum() == 0:
        return np.nan
    return (values[valid] * weights[valid]).sum() / weights[valid].sum()


def arithmetic_mean(values, weights=None):
    """Equal-weight mean across years, ignoring nan."""
    values = np.asarray(values)
    valid = ~np.isnan(values)
    if not valid.any():
        return np.nan
    return values[valid].mean()


# Aggregation method depends on the metric:
#   baseline metrics (mean, median) → simple arithmetic mean across years
#       (easiest to understand for users who prefer a plain baseline)
#   advanced metrics (diff_mean, unit_norm_diff_mean) → weighted by n_students
#       (more evidence from larger cohorts, as established by the LOO analysis)
AGGREGATION_BY_METRIC = {
    'mean':                arithmetic_mean,
    'median':              arithmetic_mean,
    'diff_mean':           weighted_mean,
    'unit_norm_diff_mean': weighted_mean,
}


def aggregate_years(metric, year_map, years_to_use):
    """Aggregate the metric over the given years using the metric's method.

    `year_map` is {year: (n_students, metric_value, reference_n)}. Returns
    (score, n_students, reference_n) where n_students is the MEDIAN number of
    students per year across the years used — the school's typical cohort size in
    this view. Median (not sum or mean) because summing across overlapping views
    is meaningless and would drift upward as years accumulate, and because the
    median is robust to anomalous years. reference_n is the minimum across the
    years THIS VIEW uses: a single_year view is only as trustworthy as that one
    year, and a loo view excludes a year that may have been the weak one.
    """
    aggregate = AGGREGATION_BY_METRIC[metric]
    used = [y for y in years_to_use if y in year_map]
    if not used:
        return np.nan, 0, 0
    vals = np.array([year_map[y][1] for y in used])
    weights = np.array([year_map[y][0] for y in used])
    n_students = int(round(np.median(weights)))
    reference_n = min(year_map[y][2] for y in used)
    return aggregate(vals, weights), n_students, reference_n


def school_views(school_years):
    """(view_kind, view_param, years) for a school's sorted years, in E8's order:
    base, loo per year (only with >= 2 years), single_year per year, last_k for
    k = 2..n-1."""
    years = list(school_years)
    n_years = len(years)
    views = [('base', None, years)]
    if n_years >= 2:
        views += [('loo', excluded, [y for y in years if y != excluded]) for excluded in years]
    views += [('single_year', year, [year]) for year in years]
    views += [('last_k', k, years[-k:]) for k in range(2, n_years)]
    return views


def composite_min(per_subject_score, per_subject_n, subjects):
    """The weakest subject per school: ({rspo: score}, {rspo: that subject's n}).

    Only schools with a score in every one of `subjects` appear. A tie goes to the
    first subject in `subjects`. The school set is built as
    set(first) & set(second) & ..., in `subjects` order - the same set operations
    the E8 notebook used, so the resulting dict iterates in the same order and the
    E8 shards keep their bytes.
    """
    common = set(per_subject_score[subjects[0]])
    for subject in subjects[1:]:
        common = common & set(per_subject_score[subject])
    composite_score, composite_n = {}, {}
    for rspo in common:
        weakest = min(subjects, key=lambda s: per_subject_score[s][rspo])
        composite_score[rspo] = per_subject_score[weakest][rspo]
        composite_n[rspo] = per_subject_n[weakest][rspo]
    return composite_score, composite_n


def composite_views(view_results, view_n, level, metric, subjects):
    """composite_min's views for one (level, metric): ({key: {rspo: score}}, {key: {rspo: n}}).

    Every (view_kind, view_param) that one of `subjects` was stored under at this
    level and metric gets a composite view under the key (level, metric,
    'composite_min', view_kind, view_param), holding composite_min over `subjects`;
    a view where no school has every subject is left out. The level must match, or
    the minimum would be taken across levels.

    The views come out in the order the subjects' views were stored in, which is
    load-bearing (spec §8.3): it is a dict, not a set, because a set of
    ('loo', 2021)-style tuples iterates in an order set by string hashing, which
    Python randomises per process, so the shards' composite_min views came out in
    a different order on every run. Nothing compares key order - the skip-if-
    unchanged writer and the validator both compare parsed values - so only this
    function, and its test, keep it.
    """
    view_keys = dict.fromkeys(
        (view_kind, view_param) for (lvl, m, subject, view_kind, view_param) in view_results
        if lvl == level and m == metric and subject in subjects)
    composite_results, composite_n = {}, {}
    for view_kind, view_param in view_keys:
        scores = {s: view_results.get((level, metric, s, view_kind, view_param), {})
                  for s in subjects}
        cohorts = {s: view_n.get((level, metric, s, view_kind, view_param), {}) for s in subjects}
        score, n = composite_min(scores, cohorts, subjects)
        if score:
            key = (level, metric, 'composite_min', view_kind, view_param)
            composite_results[key], composite_n[key] = score, n
    return composite_results, composite_n


def rank_view(score_map):
    """[(rspo, score, rank, pct, n_in_view)] for one view, in score_map's order.

    rank 1 = highest score, ties take the minimum rank. pct = ascending average
    rank / n × 100, unrounded, so 100 is best; it is None where the view ranks
    fewer than MIN_PERCENTILE_N schools (spec §5.5 d) - a percentile among 6
    schools can only be one of six values. On E8 data no view is that small.
    """
    rspos = list(score_map)
    scores = np.array([score_map[r] for r in rspos], dtype=float)
    n_in_view = len(scores)
    if not n_in_view:
        return []
    ranks = stats.rankdata(-scores, method='min').astype(int)
    pcts = stats.rankdata(scores, method='average') / n_in_view * 100
    withheld = n_in_view < MIN_PERCENTILE_N
    return [(rspo, score, int(rank), None if withheld else pct, n_in_view)
            for rspo, score, rank, pct in zip(rspos, scores, ranks, pcts, strict=True)]


def view_param_key(view_kind, view_param):
    """JSON-safe string key for a view's parameter."""
    if view_kind == 'base':
        return None  # base has no parameter — stored as flat object, not nested dict
    if pd.isna(view_param):
        return ''
    # Years and k values are integers; coerce
    try:
        return str(int(float(view_param)))
    except (TypeError, ValueError):
        return str(view_param)


def cell_payload(score, rank, pct):
    """One published shard cell: score to 4 places, rank, pct to 1 place or None."""
    withheld = pct is None or (isinstance(pct, float) and math.isnan(pct))
    return {
        'score': round(float(score), 4),
        'rank':  int(rank),
        'pct':   None if withheld else round(float(pct), 1),
    }
