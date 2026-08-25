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

MIN_REFERENCE_N = 5
MIN_PERCENTILE_N = 8


def reference_is_usable(n: int) -> bool:
    """Whether a region of `n` schools can serve as a comparison baseline."""
    return n >= MIN_REFERENCE_N


def percentile_is_meaningful(n: int) -> bool:
    """Whether a percentile within `n` schools carries usable resolution."""
    return n >= MIN_PERCENTILE_N


def suppress_diff_score(score, n: int):
    """The score, or None where the region is too small to compare within."""
    if score is None or not reference_is_usable(n):
        return None
    return score


def suppress_percentile(pct, n: int):
    """The percentile, or None where it would be granularity theatre."""
    if pct is None or not percentile_is_meaningful(n):
        return None
    return pct


def rank_label(rank: int, n: int) -> str:
    """What a region too small for a percentile shows instead."""
    return f'{rank} of {n}'
