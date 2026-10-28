"""The validator's recomputation on sparse subjects (matura spec §5.5).

E8 cannot exercise these rules - every E8 row has every subject - so the E8 run
proves only that they are no-ops there. These pin down what they do where a
subject is sat at some schools only.
"""

from pathlib import Path

from validate_export import (
    Profile,
    compute_reference_stats,
    compute_region_level,
    compute_subject_takers,
)


def source_row(rspo, teryt='1461011', year=2024, **means):
    """A matura-shaped source row: a subject key only where its mean was published."""
    return {'rspo': rspo, 'year': year, 'teryt': teryt, 'wojewodztwo': 'mazowieckie',
            **{subject: {'n': 10.0, 'mean': mean, 'median': mean}
               for subject, mean in means.items()}}


def test_a_reference_counts_the_schools_with_a_mean_in_that_subject():
    """Rule a: the reference behind chemia_r is the 3 schools with a chemia_r mean,
    not the 6 rows of the voivodeship - the minimal edit to E8's code counts 6."""
    rows = [source_row(rspo, polski=50.0, **({'chemia_r': 40.0 + rspo} if rspo < 3 else {}))
            for rspo in range(6)]
    means, sizes = compute_reference_stats(rows, 'voivodeship', ('polski', 'chemia_r'))
    assert sizes[(2024, '14')] == {'polski': 6, 'chemia_r': 3}
    assert means[(2024, '14')]['chemia_r'] == 41.0


def sparse_profile(subjects):
    return Profile(
        name='test', docs_data=Path('.'), subjects=subjects,
        reference_levels=('national', 'voivodeship'), region_levels=('voivodeship', 'powiat'),
        slider_subjects=subjects, file_budgets_kb={}, shard_budget_kb=0, sparse_subjects=True,
    )


def powiat_level(subjects, teryt_by_rspo, takers, students, diff_scores):
    """compute_region_level for two powiats of one voivodeship, from diff_mean base
    scores at the voivodeship reference (the level a powiat is scored against)."""
    views = {('voivodeship', 'diff_mean', subject, 'base', None): scores
             for subject, scores in diff_scores.items()}
    return compute_region_level(sparse_profile(subjects), 'powiat', {'1461', '1462'},
                                teryt_by_rspo, students, takers, views)


def test_subject_takers_are_the_median_over_the_years_with_a_mean():
    rows = [source_row(1, year=2024, chemia_r=40.0), source_row(1, year=2025, polski=50.0),
            source_row(1, year=2026, chemia_r=45.0)]
    rows[0]['chemia_r']['n'], rows[2]['chemia_r']['n'] = 6.0, 9.0
    assert compute_subject_takers(rows, ('polski', 'chemia_r')) == {
        'polski': {1: 10}, 'chemia_r': {1: 8}}


def test_the_parent_gate_counts_schools_with_a_mean_in_the_subject():
    """Rule c: six schools sit in the voivodeship, but only four have a chemia_r
    mean, so no powiat gets a chemia_r difference score; polski, held by all six,
    is scored. n_scored says why the chemia_r cells are empty."""
    teryt_by_rspo = {rspo: ('1461011' if rspo < 3 else '1462011') for rspo in range(6)}
    takers = {'polski': dict.fromkeys(range(6), 30), 'chemia_r': dict.fromkeys(range(4), 7)}
    level = powiat_level(('polski', 'chemia_r'), teryt_by_rspo, takers, dict.fromkeys(range(6), 30),
                         {'polski': dict.fromkeys(range(6), 0.1),
                          'chemia_r': dict.fromkeys(range(4), 0.2)})
    assert level['parent_scored']['chemia_r'] == {'14': 4}
    assert level['score']['diff_mean']['chemia_r'] == [None, None]
    assert level['score']['diff_mean']['polski'] == [0.1, 0.1]
    assert level['n_scored']['chemia_r'] == [3, 1]
    assert level['n_takers']['chemia_r'] == [21, 7]


def test_a_region_weights_its_schools_by_the_subjects_own_takers():
    """Rule b: two chemia_r schools scoring 0.0 and 1.0, with 1 and 3 takers, make
    0.75 - not the 0.5 that weighting by their equal largest cohorts would give."""
    teryt_by_rspo = {rspo: ('1461011' if rspo < 2 else '1462011') for rspo in range(6)}
    takers = {'chemia_r': {0: 1, 1: 3, 2: 5, 3: 5, 4: 5, 5: 5}}
    level = powiat_level(('chemia_r',), teryt_by_rspo, takers, dict.fromkeys(range(6), 100),
                         {'chemia_r': {0: 0.0, 1: 1.0, 2: 0.5, 3: 0.5, 4: 0.5, 5: 0.5}})
    assert level['score']['diff_mean']['chemia_r'][0] == 0.75
