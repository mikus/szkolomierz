"""Reference statistics grouped over a configurable region level."""

import numpy as np
import pandas as pd
import pytest

from school_quality.levels import (
    REFERENCE_LEVELS,
    add_level_keys,
    attach_reference,
    reference_frame,
)


def _df():
    # Two voivodeships, two powiats inside the first, two years.
    return pd.DataFrame([
        {'rspo': 1, 'year': 2025, 'teryt': '1425063', 'mean_polski': 60.0},
        {'rspo': 2, 'year': 2025, 'teryt': '1425063', 'mean_polski': 70.0},
        {'rspo': 3, 'year': 2025, 'teryt': '1401011', 'mean_polski': 80.0},
        {'rspo': 4, 'year': 2025, 'teryt': '0401011', 'mean_polski': 20.0},
        {'rspo': 1, 'year': 2026, 'teryt': '1425063', 'mean_polski': 50.0},
    ])


def test_every_level_is_defined_and_national_groups_on_year_alone():
    assert set(REFERENCE_LEVELS) == {'national', 'voivodeship', 'powiat', 'gmina'}
    assert REFERENCE_LEVELS['national'] == []


def test_level_keys_are_added_from_teryt():
    keyed = add_level_keys(_df())
    assert keyed.loc[0, 'teryt_wojewodztwo'] == '14'
    assert keyed.loc[0, 'teryt_powiat'] == '1425'
    assert keyed.loc[0, 'teryt_gmina'] == '142506'


def test_national_reference_is_the_mean_over_all_schools_that_year():
    keyed = add_level_keys(_df())
    out = attach_reference(keyed, 'mean_polski', 'national', 'ref', 'mean')
    assert out.loc[out['year'] == 2025, 'ref'].unique().tolist() == [pytest.approx(57.5)]


def test_voivodeship_reference_separates_the_two_voivodeships():
    keyed = add_level_keys(_df())
    out = attach_reference(keyed, 'mean_polski', 'voivodeship', 'ref', 'mean')
    mazowieckie = out[(out['year'] == 2025) & (out['teryt_wojewodztwo'] == '14')]
    kujawsko = out[(out['year'] == 2025) & (out['teryt_wojewodztwo'] == '04')]
    assert mazowieckie['ref'].unique().tolist() == [pytest.approx(70.0)]
    assert kujawsko['ref'].unique().tolist() == [pytest.approx(20.0)]


def test_the_reference_is_per_year_not_pooled_across_years():
    keyed = add_level_keys(_df())
    out = attach_reference(keyed, 'mean_polski', 'voivodeship', 'ref', 'mean')
    school_1 = out[out['rspo'] == 1].set_index('year')['ref']
    assert school_1.loc[2025] != school_1.loc[2026]


def test_attaching_a_reference_never_yields_all_nan():
    # The regression this whole task exists to prevent: broadcasting a
    # MultiIndex-keyed reference with .map() returns all-NaN silently.
    keyed = add_level_keys(_df())
    out = attach_reference(keyed, 'mean_polski', 'powiat', 'ref', 'mean')
    assert out['ref'].notna().all()


def test_row_count_and_order_survive_the_merge():
    keyed = add_level_keys(_df())
    out = attach_reference(keyed, 'mean_polski', 'gmina', 'ref', 'mean')
    assert len(out) == len(keyed)
    assert out['rspo'].tolist() == keyed['rspo'].tolist()


def test_median_and_mean_give_different_references_on_a_skewed_group():
    # A dedicated skewed frame: in _df() the 2025 '14' group is {60, 70, 80},
    # whose mean and median are both 70, so it could not tell the two apart -
    # an implementation ignoring `statistic` would pass.
    skewed = pd.DataFrame([
        {'rspo': 1, 'year': 2025, 'teryt': '1425063', 'mean_polski': 10.0},
        {'rspo': 2, 'year': 2025, 'teryt': '1425063', 'mean_polski': 20.0},
        {'rspo': 3, 'year': 2025, 'teryt': '1425063', 'mean_polski': 90.0},
    ])
    keyed = add_level_keys(skewed)
    as_mean = attach_reference(keyed, 'mean_polski', 'voivodeship', 'ref', 'mean')
    as_median = attach_reference(keyed, 'mean_polski', 'voivodeship', 'ref', 'median')
    assert as_mean['ref'].unique().tolist() == [pytest.approx(40.0)]
    assert as_median['ref'].unique().tolist() == [pytest.approx(20.0)]


def test_the_statistic_must_be_stated_explicitly():
    # No default: defaulting to 'mean' is how a median reference silently
    # becomes a mean one at a call site that simply forgot the argument.
    with pytest.raises(TypeError):
        attach_reference(add_level_keys(_df()), 'mean_polski', 'voivodeship', 'ref')


def test_an_unknown_statistic_is_rejected():
    with pytest.raises(ValueError, match='statistic'):
        attach_reference(add_level_keys(_df()), 'mean_polski', 'voivodeship', 'ref', 'mode')


def test_rows_with_a_missing_value_still_receive_a_reference():
    # groupby drops NaN keys, not NaN values; a school missing this subject must
    # still get its region's reference so downstream code sees NaN score, not NaN ref.
    frame = add_level_keys(_df())
    frame.loc[0, 'mean_polski'] = np.nan
    out = attach_reference(frame, 'mean_polski', 'voivodeship', 'ref', 'mean')
    assert out['ref'].notna().all()


def test_reference_frame_has_one_row_per_group():
    keyed = add_level_keys(_df())
    ref = reference_frame(keyed, 'mean_polski', 'voivodeship', 'mean')
    assert list(ref.columns) == ['year', 'teryt_wojewodztwo', 'mean_polski_reference']
    assert len(ref) == 3  # (2025, 14), (2025, 04), (2026, 14)


def test_an_unknown_level_is_rejected():
    with pytest.raises(KeyError):
        attach_reference(add_level_keys(_df()), 'mean_polski', 'district', 'ref', 'mean')
