"""Per-year reference statistics, grouped over a configurable region level.

A school's score is its distance from a reference - the mean of some population
in the same year. Which population is the choice this module parameterises:
the whole country, its voivodeship, its powiat, or its gmina.

Broadcasting the result back onto the rows is done with a merge, never with
Series.map. Against a MultiIndex-keyed Series, .map() returns all-NaN without
raising, which would silently zero out every downstream metric.
"""

import pandas as pd

from school_quality.teryt import KEY_FUNCTIONS

LEVEL_KEY_COLUMNS = {
    'voivodeship': 'teryt_wojewodztwo',
    'powiat': 'teryt_powiat',
    'gmina': 'teryt_gmina',
}

REFERENCE_LEVELS = {
    'national': [],
    'voivodeship': [LEVEL_KEY_COLUMNS['voivodeship']],
    'powiat': [LEVEL_KEY_COLUMNS['powiat']],
    'gmina': [LEVEL_KEY_COLUMNS['gmina']],
}

STATISTICS = ('mean', 'median')


def add_level_keys(df: pd.DataFrame, teryt_column: str = 'teryt') -> pd.DataFrame:
    """Return a copy with one key column per region level."""
    out = df.copy()
    for level, column in LEVEL_KEY_COLUMNS.items():
        out[column] = out[teryt_column].map(KEY_FUNCTIONS[level])
    return out


def _group_keys(level: str) -> list[str]:
    if level not in REFERENCE_LEVELS:
        raise KeyError(f'unknown reference level {level!r}; '
                       f'expected one of {sorted(REFERENCE_LEVELS)}')
    return ['year'] + REFERENCE_LEVELS[level]


def reference_frame(df: pd.DataFrame, value_column: str, level: str,
                    statistic: str) -> pd.DataFrame:
    """One row per (year, region) carrying `{value_column}_reference`.

    `statistic` has no default on purpose. The project needs both - the mean of
    school means for diff_mean, the median of school medians for diff_median -
    and a default is how a call site that simply omits the argument silently
    changes which one it gets.
    """
    if statistic not in STATISTICS:
        raise ValueError(f'statistic must be one of {STATISTICS}, got {statistic!r}')
    keys = _group_keys(level)
    grouped = df.groupby(keys, dropna=False)[value_column]
    reference = grouped.mean() if statistic == 'mean' else grouped.median()
    return reference.rename(f'{value_column}_reference').reset_index()


def attach_reference(df: pd.DataFrame, value_column: str, level: str,
                     out_column: str, statistic: str) -> pd.DataFrame:
    """Return a copy of `df` with the per-(year, region) reference in `out_column`.

    Row count and row order are preserved, so the result can be assigned straight
    back onto the caller's frame.
    """
    keys = _group_keys(level)
    reference = reference_frame(df, value_column, level, statistic)
    merged = df.merge(reference, on=keys, how='left', validate='many_to_one')
    merged[out_column] = merged[f'{value_column}_reference']
    return merged.drop(columns=[f'{value_column}_reference'])
