"""Rules that keep small regions from producing confident-looking nonsense.

Measured on 2025, scored schools only: a gmina holds a median of 3 schools, and
16.5% hold exactly one. Two consequences, enforced here rather than explained
away in help text.

A difference-based metric measures distance from a reference mean. Where the
region holds one school, that school IS the reference, so the score is
identically zero by construction. MIN_REFERENCE_N withholds those scores.

A percentile inside a population of n has granularity 1/n; at n=3 it can only be
0, 50 or 100. MIN_PERCENTILE_N withholds those, and the caller shows a plain
rank instead - "2 of 3" is more useful to a parent than a fabricated 50th
percentile, and it is true.
"""

import math

MIN_REFERENCE_N = 5
MIN_PERCENTILE_N = 8


def _is_missing(value) -> bool:
    """True for None or NaN, without depending on numpy/pandas for the check.

    `math` is the standard library, so this keeps the module free of the numpy/
    pandas dependency while still catching the numpy floats the real pipeline
    (the notebook's export loop) actually passes.
    """
    return value is None or math.isnan(value)


def reference_is_usable(n: int) -> bool:
    """Whether a region of `n` schools can serve as a comparison baseline."""
    return bool(n >= MIN_REFERENCE_N)


def percentile_is_meaningful(n: int) -> bool:
    """Whether a percentile within `n` schools carries usable resolution."""
    return bool(n >= MIN_PERCENTILE_N)


def suppress_diff_score(score, n: int) -> float | None:
    """The score, or None where missing or the region is too small to compare
    within."""
    if _is_missing(score) or not reference_is_usable(n):
        return None
    return score


def suppress_percentile(pct, n: int) -> float | None:
    """The percentile, or None where missing or it would be granularity theatre."""
    if _is_missing(pct) or not percentile_is_meaningful(n):
        return None
    return pct


def rank_label(rank: int, n: int) -> str:
    """What a region too small for a percentile shows instead."""
    return f'{rank} of {n}'
