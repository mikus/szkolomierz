"""Rolling schools up into a region, and deciding what that region may publish.

Two rules decide publication, and they gate DIFFERENT populations. Conflating
them is the easiest way to break this map, so they are separate functions with
separate arguments rather than one function with a mode flag:

  - a region's score is a distance from its PARENT's mean, so the parent's
    schools are what must be numerous enough;
  - a region's percentile is its position among its SIBLINGS, so the siblings
    are what must be numerous enough.

The two rules differ in one more way. The SCORE rule applies only to the
DIFFERENCE metrics: `mean` and `median` are raw 0-100 aggregates with no
reference population at all (spec §4.2: their score does not vary by level), so
nothing about them is undefined in a small or only-child region and nothing
about them may be blanked. The PERCENTILE rule applies to every metric, because
a percentile is a position among siblings whatever the number being ranked is -
an only child is otherwise the 100th percentile of itself.

Measured on the export population - the union across 2021-2026, which is what
the gate actually runs on, not the single-year figures spec §5.2 quotes:
gating a score on the region's own school count instead would blank 1,556 of
2,476 gminas, while the correct gate blanks none - no powiat has fewer than
five schools. Gating a percentile on school count instead would publish one for
the gminas of 241 of 380 powiats that have too few siblings for it to mean
anything.
"""

import math

import numpy as np
from scipy import stats

from school_quality.suppression import MIN_PERCENTILE_N, MIN_REFERENCE_N

WEIGHTED_METRICS = frozenset({'diff_mean', 'unit_norm_diff_mean'})
ARITHMETIC_METRICS = frozenset({'mean', 'median'})
STATISTICS = ('weighted', 'arithmetic')

# The metrics whose score is a distance from a reference population, and so the
# only ones the publication gates apply to. Same set as WEIGHTED_METRICS today;
# named separately because the two ideas are independent - one is how you
# average across schools, the other is whether the number means anything.
REFERENCED_METRICS = WEIGHTED_METRICS


def weighting_for(metric: str) -> str:
    """'weighted' or 'arithmetic' for a metric. Raises on an unknown one."""
    if metric in WEIGHTED_METRICS:
        return 'weighted'
    if metric in ARITHMETIC_METRICS:
        return 'arithmetic'
    raise KeyError(f'unknown metric {metric!r}')


def aggregate_region(scores, weights, statistic: str):
    """One region's score from its schools', or None if it has none.

    `statistic` has no default on purpose - the same reasoning as
    levels.attach_reference. A default is how a call site that forgets the
    argument silently gets the wrong kind of average.
    """
    if statistic not in STATISTICS:
        raise ValueError(f'statistic must be one of {STATISTICS}, got {statistic!r}')
    pairs = [(s, w) for s, w in zip(scores, weights)
             if s is not None and not math.isnan(s)]
    if not pairs:
        return None
    if statistic == 'arithmetic':
        return sum(s for s, _ in pairs) / len(pairs)
    total = sum(w for _, w in pairs)
    if not total:
        # Every school reports zero pupils - fall back rather than divide by
        # zero. Rare, but it happens in a year a school reports no cohort.
        return sum(s for s, _ in pairs) / len(pairs)
    return sum(s * w for s, w in pairs) / total


def rank_and_percentile(scores):
    """Ranks and percentiles for one level, over the regions that have a score.

    The convention is deliberately identical to the school path in code-cell
    #33: rank 1 = highest score with ties taking the minimum rank, and
    percentile = ascending average rank / n * 100, so 100 is best. Two
    different definitions of "percentile" rendered into the same column is a
    defect no amount of looking at the map would reveal.

    `scores` may hold None for a region with nothing to rank. Those come back
    None in both lists and are not counted in n.
    """
    present = [(i, s) for i, s in enumerate(scores)
               if s is not None and not math.isnan(s)]
    ranks = [None] * len(scores)
    pcts = [None] * len(scores)
    if not present:
        return ranks, pcts
    arr = np.array([s for _, s in present], dtype=float)
    n = len(arr)
    computed_ranks = stats.rankdata(-arr, method='min').astype(int)
    computed_pcts = stats.rankdata(arr, method='average') / n * 100
    for (i, _), rk, pc in zip(present, computed_ranks, computed_pcts):
        ranks[i] = int(rk)
        pcts[i] = round(float(pc), 1)
    return ranks, pcts


def region_score_publishable(metric: str, parent_school_count: int,
                             sibling_count: int) -> bool:
    """Whether a region may publish this metric's score.

    `metric` is required, and it is the whole point of the function: without
    it the caller applies a difference-metric rule to `mean` and `median` and
    blanks them for every small region, including all 66 city gminas.
    """
    if metric not in REFERENCED_METRICS:
        return True         # no reference population, nothing to be too small
    if sibling_count <= 1:
        return False        # its parent is itself; see the module docstring
    return parent_school_count >= MIN_REFERENCE_N


def region_percentile_publishable(sibling_count: int) -> bool:
    """Whether a region's position among its siblings carries usable resolution.

    Deliberately NOT metric-aware, unlike region_score_publishable. A percentile
    is a position within the sibling population, so it is the siblings that must
    be numerous enough - and that is just as true of `mean`, whose score needs no
    reference population at all. An only child is otherwise the 100th percentile
    of itself for `mean`, which is a number the map would happily colour.
    """
    return sibling_count >= MIN_PERCENTILE_N
