#!/usr/bin/env python3
"""Validate the map/ranking JSON exports against an INDEPENDENT recomputation.

This script does NOT import the notebook. It re-reads the source OKE xlsx files,
re-derives every per-year metric, every view aggregate (base / single_year / loo /
last_k), composite_min, every rank/percentile, and the colour-scale metadata
(sigma, centre, slider ranges) from scratch, then checks the JSON the app actually
serves (docs/data/schools-base.json + schools-{metric}.json) against it.

It restricts the recomputation to the JSON's own `years_in_data`, so it validates
whatever export it is given (current or a year-pinned one) even when newer xlsx
files are already present in the data directory.

The JSON is kept as plain dicts keyed by rspo (no DataFrame) — convenient for the
nested score lookups.

Checks
  A. Subject raw values:  JSON single_year of metric 'mean'/'median' == the
     mean/median read straight from the xlsx for that (school, subject, year).
  B. Aggregates:          recomputed score == JSON score for every
     (metric, subject, view) — single_year / loo / base / last_k + composite_min.
  C. Completeness:        the set of (metric, subject, view, param, school) keys
     in the JSON equals the recomputed set (nothing dropped or invented).
  D. base.json:           its scores[metric][subject] == the per-metric file's
     base view (the two exports must agree).
  E. Ranks / percentiles: per view population, the stored rank/pct match rankdata
     over the recomputed scores (min-rank ties, average-rank percentile), up to a
     near-tie tolerance (the export ranks full-precision floats).
  F. Colour-scale metadata: recomputed sigma / centre / slider_ranges == metadata.
  G. Class spread:        bucketing base scores into A/B/C by ±0.33sigma (the map's
     colour rule) leaves no class empty (a never-shown colour would be a red flag).

Usage
  uv run python scripts/validate_export.py
  uv run python scripts/validate_export.py --data-dir data/egzamin-osmoklasisty \
      --docs-data docs/data
Exit code 0 if everything passes, 1 otherwise.
"""

from __future__ import annotations

import argparse
import bisect
import json
import re
import sys
import unicodedata
from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path
from typing import TypedDict

import numpy as np
import pandas as pd
from scipy import stats

# Type aliases — document the SHAPE of the nested data once; the variable names
# document the ROLE at each use site (so the two don't repeat each other).
Rspo = int
Year = int
Subject = str  # 'polski' | 'matematyka' | 'angielski' | 'composite_min'
Metric = str  # 'mean' | 'median' | 'diff_mean' | 'unit_norm_diff_mean'
ViewKind = str  # 'base' | 'loo' | 'single_year' | 'last_k'
# A view is identified by (metric, subject, view_kind, param); param is None for
# base, else an int (the excluded/selected year, or k).
ViewKey = tuple[Metric, Subject, ViewKind, 'int | None']
ViewScores = dict[ViewKey, dict[Rspo, float]]


class ScoreCell(TypedDict):
    """One exported value: the aggregate score, its rank (1 = best) and percentile."""

    score: float
    rank: int
    pct: float


CORE_SUBJECTS = ['polski', 'matematyka', 'angielski']
ALL_SUBJECTS = CORE_SUBJECTS + ['composite_min']
EXPORT_METRICS = ['mean', 'median', 'diff_mean', 'unit_norm_diff_mean']
WEIGHTED_METRICS = {'diff_mean', 'unit_norm_diff_mean'}  # the rest use a plain mean
YEAR_FILE_RE = re.compile(r'^\d{4}')

# JSON stores scores at 4 dp and percentiles at 1 dp. Comparing the rounded JSON
# value to the UNROUNDED recomputation, the largest legitimate gap is half a
# rounding unit (5e-5) plus float noise — so 1e-4 / 0.06 are tight but safe.
SCORE_TOL = 1e-4
PCT_TOL = 0.06
CLASS_BOUND = 0.33  # +-0.33 sigma, matching the map/ranking colour rule
# Scores within this are treated as a tie when validating ranks: the export ranks
# full-precision floats, so two schools equal to ~13 sig figs (e.g. from (v*n)/n
# round-off) get an arbitrary order an independent recompute can't reproduce
# bit-for-bit. Genuine score precision is ~1e-4, so 1e-9 separates real from noise.
TIE_EPS = 1e-9

# Source-xlsx column names (after normalisation), needed to read the SAS sheet.
N_COL, MEAN_COL, MEDIAN_COL = 'liczba zdajacych', 'wynik sredni (%)', 'mediana (%)'
SCOPE_VOIVODESHIP = 'Mazowieckie'  # mirrors the notebook; both drop when spec step 3 lands


# ── xlsx reading (independent re-implementation of the notebook loader) ───────


def normalize_text(text: str) -> str:
    """Lower-case, strip Polish diacritics (incl. l-stroke), collapse whitespace —
    the same header normalisation the notebook applies, so columns line up."""
    text = unicodedata.normalize('NFKD', str(text))
    text = ''.join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace('ł', 'l').replace('Ł', 'L').replace('\n', ' ')
    return re.sub(r'\s+', ' ', text).lower().strip()


def clean_header_value(value) -> str:
    """A raw header cell -> trimmed string; pandas' 'Unnamed:'/'nan' fillers -> ''."""
    if value is None:
        return ''
    text = str(value)
    if text.startswith('Unnamed:') or text == 'nan':
        return ''
    return re.sub(r'\s+', ' ', text.replace('\n', ' ')).strip()


def normalize_columns(columns: pd.MultiIndex) -> pd.MultiIndex:
    """Two-level SAS header -> (subject, metric) MultiIndex. Level-0 (subject) is
    forward-filled across the merged header cells; blank groups become 'meta'."""
    subjects, metrics = [], []
    last_subject = 'meta'
    for raw_subject, raw_metric in columns.to_list():
        subject = clean_header_value(raw_subject)
        metric = clean_header_value(raw_metric)
        if subject:
            last_subject = normalize_text(subject).removeprefix('jezyk ')
        subjects.append(last_subject)
        metrics.append(normalize_text(metric) if metric else 'value')
    return pd.MultiIndex.from_arrays([subjects, metrics], names=['subject', 'metric'])


def read_clean_rows(data_dir: Path) -> list[dict]:
    """Read the source xlsx and return one dict per surviving (school, year) row:
    {'rspo', 'year', <subject>: {'n', 'mean', 'median'}}.

    Cleaning matches the notebook: keep n_polski > 0 and require mean AND median
    present for all three core subjects (rows missing any are dropped). Column
    extraction is vectorised; a missing sheet/column fails with a clear message.
    """
    files = sorted(
        p
        for p in data_dir.glob('*.xlsx*')
        if not p.name.startswith('.') and YEAR_FILE_RE.match(p.name)
    )
    if not files:
        sys.exit(f'No source xlsx files (matching ^\\d{{4}}) in {data_dir}')

    needed = [(subject, col) for subject in CORE_SUBJECTS for col in (N_COL, MEAN_COL, MEDIAN_COL)]
    per_file_frames = []
    for path in files:
        year = int(YEAR_FILE_RE.match(path.name).group(0))
        try:
            sheet = pd.read_excel(path, sheet_name='SAS', header=[0, 1])
        except ValueError as exc:
            sys.exit(f"Cannot read sheet 'SAS' in {path.name}: {exc}")
        sheet.columns = normalize_columns(sheet.columns)
        missing = [c for c in [('meta', 'rspo'), ('meta', 'wojewodztwo - nazwa')] + needed
                   if c not in sheet.columns]
        if missing:
            sys.exit(f'{path.name}: expected columns not found after normalisation: {missing}')

        column_data = {
            'rspo': pd.to_numeric(sheet[('meta', 'rspo')], errors='coerce'),
            'wojewodztwo': sheet[('meta', 'wojewodztwo - nazwa')].astype(str).str.strip(),
            'year': year,
        }
        for subject in CORE_SUBJECTS:
            column_data[f'n_{subject}'] = pd.to_numeric(sheet[(subject, N_COL)], errors='coerce')
            column_data[f'mean_{subject}'] = pd.to_numeric(
                sheet[(subject, MEAN_COL)], errors='coerce'
            )
            column_data[f'median_{subject}'] = pd.to_numeric(
                sheet[(subject, MEDIAN_COL)], errors='coerce'
            )
        per_file_frames.append(pd.DataFrame(column_data))

    df = pd.concat(per_file_frames, ignore_index=True)
    keep = df['rspo'].notna() & df['n_polski'].notna() & (df['n_polski'] > 0)
    for subject in CORE_SUBJECTS:
        keep &= df[f'mean_{subject}'].notna() & df[f'median_{subject}'].notna()
    keep &= df['wojewodztwo'] == SCOPE_VOIVODESHIP
    df = df[keep]

    rows = []
    for record in df.to_dict('records'):
        row = {'rspo': int(record['rspo']), 'year': int(record['year'])}
        for subject in CORE_SUBJECTS:
            row[subject] = {
                'n': record[f'n_{subject}'],
                'mean': record[f'mean_{subject}'],
                'median': record[f'median_{subject}'],
            }
        rows.append(row)
    return rows


# ── independent metric / view / rank recomputation ──────────────────────────


def compute_voivodeship_means(rows: list[dict]) -> dict[Subject, dict[Year, float]]:
    """subject -> year -> mean over schools of mean_subject, on the clean rows —
    the per-year reference that diff_mean / unit_norm_diff_mean subtract off."""
    subject_to_year_to_means: dict[Subject, dict[Year, list]] = {
        subject: defaultdict(list) for subject in CORE_SUBJECTS
    }
    for row in rows:
        for subject in CORE_SUBJECTS:
            subject_to_year_to_means[subject][row['year']].append(row[subject]['mean'])
    return {
        subject: {year: float(np.mean(means)) for year, means in by_year.items()}
        for subject, by_year in subject_to_year_to_means.items()
    }


def compute_per_year_values(
    rows: list[dict],
    subject_to_year_to_voivodeship_mean: dict[Subject, dict[Year, float]],
) -> dict[tuple[Rspo, Subject], dict[Year, dict[str, float]]]:
    """(rspo, subject) -> year -> {'n', 'mean', 'median', 'diff_mean',
    'unit_norm_diff_mean'} for valid rows (n > 0 and the metric defined).
    unit_norm uses the signed ceiling/floor normalisation to [-1, +1]."""
    per_year_by_school_subject: dict[tuple, dict[Year, dict]] = defaultdict(dict)
    for row in rows:
        year = row['year']
        for subject in CORE_SUBJECTS:
            n = row[subject]['n']
            mean = row[subject]['mean']
            median = row[subject]['median']
            if not (pd.notna(n) and n > 0):
                continue
            voivodeship_mean = subject_to_year_to_voivodeship_mean[subject][year]
            diff_mean = mean - voivodeship_mean
            if diff_mean >= 0:
                denom = (100 - voivodeship_mean) if (100 - voivodeship_mean) > 0 else 1
            else:
                denom = voivodeship_mean if voivodeship_mean > 0 else 1
            per_year_by_school_subject[(row['rspo'], subject)][year] = {
                'n': n,
                'mean': mean,
                'median': median,
                'diff_mean': diff_mean,
                'unit_norm_diff_mean': diff_mean / denom,
            }
    return per_year_by_school_subject


def aggregate_across_years(
    metric: Metric, year_to_values: dict[Year, dict[str, float]], years_used
) -> float:
    """Aggregate a metric across `years_used` for one school. Weighted by the
    student count for diff_mean / unit_norm_diff_mean, a plain mean otherwise.
    NaN inputs (and zero-weight years for the weighted case) are ignored; an empty
    selection returns NaN."""
    values = np.array([year_to_values[y][metric] for y in years_used if y in year_to_values])
    weights = np.array([year_to_values[y]['n'] for y in years_used if y in year_to_values])
    if len(values) == 0:
        return np.nan
    if metric in WEIGHTED_METRICS:
        valid = ~(np.isnan(values) | np.isnan(weights)) & (weights > 0)
        if not valid.any() or weights[valid].sum() == 0:
            return np.nan
        return float((values[valid] * weights[valid]).sum() / weights[valid].sum())
    valid = ~np.isnan(values)
    return float(values[valid].mean()) if valid.any() else np.nan


def compute_view_scores(
    rows: list[dict],
    subject_to_year_to_voivodeship_mean: dict[Subject, dict[Year, float]],
) -> ViewScores:
    """(metric, subject, view_kind, param) -> {rspo: score} for all metrics, core
    subjects, and composite_min. Each school's views use only its own years: base
    (all), loo (one excluded, >= 2 years), single_year (each), last_k (most recent
    k, k = 2..n-1). composite_min = min over the 3 core subjects, for the schools
    present in all three for that view."""
    per_year_by_school_subject = compute_per_year_values(rows, subject_to_year_to_voivodeship_mean)
    view_key_to_rspo_scores: dict[tuple, dict[Rspo, float]] = defaultdict(dict)

    for metric in EXPORT_METRICS:
        for (rspo, subject), year_to_values in per_year_by_school_subject.items():
            years = sorted(year_to_values)
            n_years = len(years)

            def store(view_kind, param, years_used):
                score = aggregate_across_years(metric, year_to_values, years_used)
                if not np.isnan(score):
                    view_key_to_rspo_scores[(metric, subject, view_kind, param)][rspo] = score

            store('base', None, years)
            if n_years >= 2:
                for excluded in years:
                    store('loo', excluded, [y for y in years if y != excluded])
            for year in years:
                store('single_year', year, [year])
            for k in range(2, n_years):
                store('last_k', k, years[-k:])

    metric_to_view_params: dict[Metric, set] = defaultdict(set)
    for metric, subject, view_kind, param in list(view_key_to_rspo_scores):
        if subject in CORE_SUBJECTS:
            metric_to_view_params[metric].add((view_kind, param))
    for metric, view_params in metric_to_view_params.items():
        for view_kind, param in view_params:
            per_subject = {
                subject: view_key_to_rspo_scores.get((metric, subject, view_kind, param), {})
                for subject in CORE_SUBJECTS
            }
            shared_rspos = (
                set(per_subject['polski'])
                & set(per_subject['matematyka'])
                & set(per_subject['angielski'])
            )
            for rspo in shared_rspos:
                view_key_to_rspo_scores[(metric, 'composite_min', view_kind, param)][rspo] = min(
                    per_subject[subject][rspo] for subject in CORE_SUBJECTS
                )
    return view_key_to_rspo_scores


def compute_ranks_and_percentiles(
    rspo_to_score: dict[Rspo, float],
) -> dict[Rspo, tuple[int, float]]:
    """rspo -> (rank, percentile) with min-rank (1 = highest score) and
    average-rank percentile (100 = highest), matching the export."""
    rspos = list(rspo_to_score)
    scores = np.array([rspo_to_score[rspo] for rspo in rspos])
    n = len(scores)
    ranks = stats.rankdata(-scores, method='min').astype(int)
    percentiles = stats.rankdata(scores, method='average') / n * 100
    return {
        rspo: (int(rank), float(percentile))
        for rspo, rank, percentile in zip(rspos, ranks, percentiles)
    }


def acceptable_rank_range(score: float, scores_ascending: list[float]):
    """The (rank_lo, rank_hi, pct_lo, pct_hi) a school may legitimately occupy
    given near-ties. The export ranks full-precision floats, so schools equal to
    within TIE_EPS get an arbitrary order an independent recompute can't reproduce.
    Schools scoring clearly higher (> score + TIE_EPS) sit above for sure; those
    clearly lower sit below; anyone within TIE_EPS may fall either side."""
    n = len(scores_ascending)
    n_better = n - bisect.bisect_right(scores_ascending, score + TIE_EPS)
    n_worse = bisect.bisect_left(scores_ascending, score - TIE_EPS)
    return n_better + 1, n - n_worse, n_worse / n * 100, (n - n_better) / n * 100


# ── JSON loading (kept as dicts keyed by rspo) ──────────────────────────────


def load_json_exports(
    docs_data: Path,
) -> tuple[dict, dict[Rspo, dict], dict[Metric, dict[Rspo, dict]]]:
    """Load the served JSON as dicts keyed by rspo. Returns
    (metadata, rspo_to_school_base_data, metric_to_rspo_to_school_data)."""
    base_path = docs_data / 'schools-base.json'
    if not base_path.exists():
        sys.exit(f'Missing {base_path}')
    base = json.loads(base_path.read_text())
    rspo_to_school_base_data = {int(school['rspo']): school for school in base['schools']}
    metric_to_rspo_to_school_data = {}
    for metric in EXPORT_METRICS:
        path = docs_data / f'schools-{metric}.json'
        if not path.exists():
            sys.exit(f'Missing {path}')
        payload = json.loads(path.read_text())
        metric_to_rspo_to_school_data[metric] = {
            int(rspo): school for rspo, school in payload['schools'].items()
        }
    return base['metadata'], rspo_to_school_base_data, metric_to_rspo_to_school_data


def iter_json_views(
    metric_to_rspo_to_school_data: dict[Metric, dict[Rspo, dict]],
) -> Iterator[tuple[Metric, Subject, ViewKind, int | None, Rspo, ScoreCell]]:
    """Yield (metric, subject, view_kind, param, rspo, cell) for every score cell
    in the per-metric files (base param is None, others are ints)."""
    for metric, schools in metric_to_rspo_to_school_data.items():
        for rspo, school in schools.items():
            for subject, subject_views in school.items():
                if not isinstance(subject_views, dict):
                    continue
                if isinstance(subject_views.get('base'), dict):
                    yield metric, subject, 'base', None, rspo, subject_views['base']
                for view_kind in ('single_year', 'loo', 'last_k'):
                    for param, cell in (subject_views.get(view_kind) or {}).items():
                        yield metric, subject, view_kind, int(param), rspo, cell


# ── reporting ────────────────────────────────────────────────────────────────


class Report:
    """Collects PASS/FAIL lines and a running comparison count; tracks whether any
    check failed so main() can set the exit code."""

    def __init__(self):
        self.failures = 0
        self.checked = 0

    def section(self, name: str):
        print(f'\n── {name} ' + '─' * max(0, 60 - len(name)))

    def ok(self, msg: str):
        print(f'  PASS  {msg}')

    def fail(self, msg: str, examples=None):
        self.failures += 1
        print(f'  FAIL  {msg}')
        for example in (examples or [])[:5]:
            print(f'          {example}')


def within_tolerance(a, b, tol: float) -> bool:
    """True if two numbers are finite and within `tol` of each other."""
    return a is not None and b is not None and abs(a - b) <= tol


# ── checks ───────────────────────────────────────────────────────────────────


def check_subjects_vs_xlsx(rows, metric_to_rspo_to_school_data, rep: Report):
    """A — the single-year scores of the 'mean'/'median' metrics must equal the
    mean/median read straight from the xlsx (ties the JSON to the raw source)."""
    rep.section('A. Subject raw values vs xlsx (single_year of mean/median)')
    rspo_year_to_row = {(row['rspo'], row['year']): row for row in rows}
    for metric, field in (('mean', 'mean'), ('median', 'median')):
        mismatches, n, max_diff = [], 0, 0.0
        for (rspo, year), row in rspo_year_to_row.items():
            school = metric_to_rspo_to_school_data[metric].get(rspo)
            if not school:
                continue
            for subject in CORE_SUBJECTS:
                cell = school.get(subject, {}).get('single_year', {}).get(str(year))
                if cell is None:
                    continue
                n += 1
                max_diff = max(max_diff, abs(cell['score'] - row[subject][field]))
                if not within_tolerance(cell['score'], row[subject][field], SCORE_TOL):
                    mismatches.append(
                        f'rspo={rspo} {subject} {year}: '
                        f'json={cell["score"]} xlsx={row[subject][field]}'
                    )
        rep.checked += n
        if mismatches:
            rep.fail(
                f'metric={metric}: {len(mismatches)}/{n} single-year values differ from xlsx',
                mismatches,
            )
        else:
            rep.ok(
                f'metric={metric}: all {n} single-year values match the xlsx (max delta {max_diff:.2e})'
            )


def check_aggregates(
    view_key_to_rspo_scores: ViewScores, metric_to_rspo_to_school_data, rep: Report
):
    """B — every JSON aggregate score (all metrics x subjects x views) must equal
    the independent recomputation. Reports the largest deviation per view kind."""
    rep.section('B. Aggregates recomputed from xlsx vs JSON (all views)')
    kind_to_stats = defaultdict(lambda: [0, 0, 0.0])  # view_kind -> [checked, failed, max_diff]
    examples = defaultdict(list)
    for metric, subject, view_kind, param, rspo, cell in iter_json_views(
        metric_to_rspo_to_school_data
    ):
        recomputed = view_key_to_rspo_scores.get((metric, subject, view_kind, param), {}).get(rspo)
        kind_to_stats[view_kind][0] += 1
        if recomputed is None:
            kind_to_stats[view_kind][1] += 1
            examples[view_kind].append(
                f'{metric}/{subject}/{view_kind}/{param} rspo={rspo}: missing in recompute'
            )
            continue
        diff = abs(cell['score'] - recomputed)
        kind_to_stats[view_kind][2] = max(kind_to_stats[view_kind][2], diff)
        if diff > SCORE_TOL:
            kind_to_stats[view_kind][1] += 1
            examples[view_kind].append(
                f'{metric}/{subject}/{view_kind}/{param} rspo={rspo}: '
                f'json={cell["score"]} recomp={recomputed:.6f}'
            )
    for view_kind, (checked, failed, max_diff) in sorted(kind_to_stats.items()):
        rep.checked += checked
        if failed:
            rep.fail(
                f'{view_kind}: {failed}/{checked} scores differ (max delta {max_diff:.2e})',
                examples[view_kind],
            )
        else:
            rep.ok(f'{view_kind}: all {checked} aggregate scores match (max delta {max_diff:.2e})')


def check_completeness(
    view_key_to_rspo_scores: ViewScores, metric_to_rspo_to_school_data, rep: Report
):
    """C — the JSON and the recomputation must contain exactly the same set of
    (metric, subject, view, param, school) score cells (nothing dropped/invented)."""
    rep.section('C. Completeness: JSON view set == recomputed view set')
    json_keys = {
        (metric, subject, view_kind, param, rspo)
        for metric, subject, view_kind, param, rspo, _ in iter_json_views(
            metric_to_rspo_to_school_data
        )
    }
    recomputed_keys = {
        (metric, subject, view_kind, param, rspo)
        for (metric, subject, view_kind, param), rspo_to_score in view_key_to_rspo_scores.items()
        for rspo in rspo_to_score
    }
    only_json = json_keys - recomputed_keys
    only_recomputed = recomputed_keys - json_keys
    rep.checked += len(json_keys | recomputed_keys)
    if only_json or only_recomputed:
        examples = [f'in JSON only: {key}' for key in list(only_json)[:3]]
        examples += [f'in recompute only: {key}' for key in list(only_recomputed)[:3]]
        rep.fail(
            f'{len(only_json)} JSON-only + {len(only_recomputed)} recompute-only cells', examples
        )
    else:
        rep.ok(f'identical key sets ({len(json_keys):,} score cells)')


def check_base_consistency(rspo_to_school_base_data, metric_to_rspo_to_school_data, rep: Report):
    """D — schools-base.json and the per-metric files describe the same export, so
    their base score/rank/pct for each (school, metric, subject) must agree."""
    rep.section('D. base.json scores == per-metric file base view')
    mismatches, n = [], 0
    for rspo, school in rspo_to_school_base_data.items():
        for metric, by_subject in school['scores'].items():
            for subject, base_cell in by_subject.items():
                per_metric_base = (
                    metric_to_rspo_to_school_data.get(metric, {})
                    .get(rspo, {})
                    .get(subject, {})
                    .get('base')
                )
                if per_metric_base is None:
                    mismatches.append(
                        f'rspo={rspo} {metric}/{subject}: base entry missing from per-metric file'
                    )
                    continue
                n += 1
                if (
                    base_cell['rank'] != per_metric_base['rank']
                    or not within_tolerance(
                        base_cell['score'], per_metric_base['score'], SCORE_TOL
                    )
                    or not within_tolerance(base_cell['pct'], per_metric_base['pct'], PCT_TOL)
                ):
                    mismatches.append(
                        f'rspo={rspo} {metric}/{subject}: '
                        f'base={base_cell} perMetric={per_metric_base}'
                    )
    rep.checked += n
    if mismatches:
        rep.fail(f'{len(mismatches)} base entries disagree between the two files', mismatches)
    else:
        rep.ok(f'all {n} base entries agree between schools-base.json and the per-metric files')


def check_ranks(view_key_to_rspo_scores: ViewScores, metric_to_rspo_to_school_data, rep: Report):
    """E — per view population, the stored rank/pct must match rankdata over the
    recomputed (full-precision) scores, accepting any order among near-ties (see
    acceptable_rank_range)."""
    rep.section('E. Ranks/percentiles match rankdata over the recomputed scores')
    view_to_stored_rank_pct = defaultdict(dict)  # view_key -> {rspo: (rank, pct)}
    view_to_json_population = defaultdict(set)  # view_key -> {rspo}
    for metric, subject, view_kind, param, rspo, cell in iter_json_views(
        metric_to_rspo_to_school_data
    ):
        view_key = (metric, subject, view_kind, param)
        view_to_stored_rank_pct[view_key][rspo] = (cell['rank'], cell['pct'])
        view_to_json_population[view_key].add(rspo)

    pop_fail = rank_fail = pct_fail = total = ambiguous = 0
    examples = []
    for view_key, stored_rank_pct in view_to_stored_rank_pct.items():
        recomputed_scores = view_key_to_rspo_scores.get(view_key, {})
        if set(recomputed_scores) != view_to_json_population[view_key]:
            pop_fail += 1
            if len(examples) < 5:
                examples.append(
                    f'{view_key}: population differs '
                    f'(json-only {len(view_to_json_population[view_key] - set(recomputed_scores))}, '
                    f'recomp-only {len(set(recomputed_scores) - view_to_json_population[view_key])})'
                )
            continue
        scores_ascending = sorted(recomputed_scores.values())
        for rspo, score in recomputed_scores.items():
            total += 1
            rank_lo, rank_hi, pct_lo, pct_hi = acceptable_rank_range(score, scores_ascending)
            if rank_lo != rank_hi:
                ambiguous += 1
            stored_rank, stored_pct = stored_rank_pct[rspo]
            if not (rank_lo <= stored_rank <= rank_hi):
                rank_fail += 1
                if len(examples) < 5:
                    examples.append(
                        f'{view_key} rspo={rspo}: '
                        f'json_rank={stored_rank} not in [{rank_lo},{rank_hi}]'
                    )
                continue
            if not (pct_lo - PCT_TOL <= stored_pct <= pct_hi + PCT_TOL):
                pct_fail += 1
                if len(examples) < 5:
                    examples.append(f'{view_key} rspo={rspo}: json_pct={stored_pct} out of range')
    rep.checked += total
    if pop_fail or rank_fail or pct_fail:
        rep.fail(
            f'{pop_fail} population + {rank_fail} rank + {pct_fail} pct mismatches '
            f'(of {total} ranked)',
            examples,
        )
    else:
        rep.ok(
            f'all {total} (rank, pct) values consistent with the scores '
            f'({ambiguous:,} within near-tie tolerance)'
        )


def check_metadata(metadata, view_key_to_rspo_scores: ViewScores, rep: Report):
    """F — recompute the colour-scale parameters and compare to the JSON metadata:
    sigma = std (sample) of base scores; centre = mean for mean/median and
    composite_min, 0 for the diff-based metrics; slider_ranges (min/max/p1/p99)
    from the base composite_min distribution. These drive the map's colours and
    value filter, so a wrong one would mis-colour the map silently."""
    rep.section('F. Colour-scale metadata (sigma / centre / slider_ranges)')
    sigma, centre = metadata['sigma'], metadata['sigma_centre']
    slider_ranges = metadata.get('slider_ranges', {})
    mismatches, n = [], 0

    for metric in EXPORT_METRICS:
        for subject in ALL_SUBJECTS:
            base_scores = np.array(
                list(view_key_to_rspo_scores.get((metric, subject, 'base', None), {}).values())
            )
            if len(base_scores) < 2:
                continue
            n += 1
            expected_sigma = round(float(base_scores.std(ddof=1)), 4)
            expected_centre = (
                round(float(base_scores.mean()), 4)
                if subject == 'composite_min' or metric in ('mean', 'median')
                else 0.0
            )
            if not within_tolerance(sigma[metric][subject], expected_sigma, SCORE_TOL):
                mismatches.append(
                    f'sigma {metric}/{subject}: '
                    f'json={sigma[metric][subject]} recomp={expected_sigma}'
                )
            if not within_tolerance(centre[metric][subject], expected_centre, SCORE_TOL):
                mismatches.append(
                    f'centre {metric}/{subject}: '
                    f'json={centre[metric][subject]} recomp={expected_centre}'
                )

    for metric in EXPORT_METRICS:
        base_scores = np.array(
            list(view_key_to_rspo_scores.get((metric, 'composite_min', 'base', None), {}).values())
        )
        if len(base_scores) == 0 or metric not in slider_ranges:
            continue
        n += 1
        expected = {
            'min': round(float(base_scores.min()), 4),
            'max': round(float(base_scores.max()), 4),
            'p1': round(float(np.quantile(base_scores, 0.01)), 4),
            'p99': round(float(np.quantile(base_scores, 0.99)), 4),
        }
        for field, value in expected.items():
            if not within_tolerance(slider_ranges[metric].get(field), value, SCORE_TOL):
                mismatches.append(
                    f'slider {metric}.{field}: '
                    f'json={slider_ranges[metric].get(field)} recomp={value}'
                )

    rep.checked += n
    if mismatches:
        rep.fail(f'{len(mismatches)} metadata mismatches', mismatches)
    else:
        rep.ok(f'sigma, centre and slider_ranges match the recomputation ({n} groups)')


def check_class_spread(metadata, rspo_to_school_base_data, rep: Report):
    """G — bucket the base scores into A / B / C by the map's +-0.33 sigma rule and
    flag any EMPTY class: a colour the map can never show usually means a
    degenerate centre/sigma (e.g. the old 5-class scheme left median-English's top
    class empty)."""
    rep.section('G. Class A/B/C spread is non-degenerate (base)')
    sigma, centre = metadata['sigma'], metadata['sigma_centre']
    empty_classes, n = [], 0
    for metric in EXPORT_METRICS:
        for subject in ALL_SUBJECTS:
            subject_sigma, subject_centre = sigma[metric][subject], centre[metric][subject]
            if not subject_sigma:
                continue
            n += 1
            class_counts = {'A': 0, 'B': 0, 'C': 0}
            for school in rspo_to_school_base_data.values():
                cell = school['scores'].get(metric, {}).get(subject)
                if not cell:
                    continue
                score = cell['score']
                school_class = (
                    'A'
                    if score > subject_centre + CLASS_BOUND * subject_sigma
                    else 'C'
                    if score < subject_centre - CLASS_BOUND * subject_sigma
                    else 'B'
                )
                class_counts[school_class] += 1
            for school_class, count in class_counts.items():
                if count == 0:
                    empty_classes.append(
                        f'{metric}/{subject}: class {school_class} empty ({class_counts})'
                    )
    rep.checked += n
    if empty_classes:
        rep.fail(
            f'{len(empty_classes)} (metric, subject) groups have an empty class', empty_classes
        )
    else:
        rep.ok(f'all {n} (metric, subject) groups populate every class A/B/C')


# ── main ─────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    """Command-line arguments: where the source xlsx and the served JSON live."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        '--data-dir',
        default='data/egzamin-osmoklasisty',
        type=Path,
        help='directory with the source OKE xlsx files',
    )
    parser.add_argument(
        '--docs-data',
        default='docs/data',
        type=Path,
        help='directory with schools-base.json and schools-{metric}.json',
    )
    return parser


def filter_rows_to_json_years(all_rows: list[dict], json_years: set[Year]) -> list[dict]:
    """Keep only the rows for the years the JSON was built from, so the
    recomputation matches the export even when newer xlsx files are present.
    Exits if a year the JSON used is missing from the xlsx; notes ignored extras."""
    xlsx_years = {row['year'] for row in all_rows}
    missing_years = json_years - xlsx_years
    if missing_years:
        sys.exit(
            f'The JSON was built from years {sorted(json_years)} but the xlsx is '
            f'missing {sorted(missing_years)} — cannot validate.'
        )
    rows = [row for row in all_rows if row['year'] in json_years]
    extra_years = xlsx_years - json_years
    note = f' (ignoring extra xlsx years {sorted(extra_years)})' if extra_years else ''
    print(f'  {len(rows):,} clean (school, year) rows over years {sorted(json_years)}{note}')
    return rows


def main():
    args = build_parser().parse_args()

    print('Loading JSON exports …')
    metadata, rspo_to_school_base_data, metric_to_rspo_to_school_data = load_json_exports(
        args.docs_data
    )
    json_years = set(metadata['years_in_data'])
    print(
        f'  base.json: {len(rspo_to_school_base_data):,} schools; years {sorted(json_years)}; '
        f'metrics {list(metric_to_rspo_to_school_data)}'
    )

    print('Reading source xlsx …')
    rows = filter_rows_to_json_years(read_clean_rows(args.data_dir), json_years)

    print('Recomputing metrics / views / ranks / metadata independently …')
    subject_to_year_to_voivodeship_mean = compute_voivodeship_means(rows)
    view_key_to_rspo_scores = compute_view_scores(rows, subject_to_year_to_voivodeship_mean)

    rep = Report()
    check_subjects_vs_xlsx(rows, metric_to_rspo_to_school_data, rep)
    check_aggregates(view_key_to_rspo_scores, metric_to_rspo_to_school_data, rep)
    check_completeness(view_key_to_rspo_scores, metric_to_rspo_to_school_data, rep)
    check_base_consistency(rspo_to_school_base_data, metric_to_rspo_to_school_data, rep)
    check_ranks(view_key_to_rspo_scores, metric_to_rspo_to_school_data, rep)
    check_metadata(metadata, view_key_to_rspo_scores, rep)
    check_class_spread(metadata, rspo_to_school_base_data, rep)

    print(f'\n{"=" * 64}')
    if rep.failures == 0:
        print(f'ALL CHECKS PASSED  ({rep.checked:,} comparisons)')
        return 0
    print(f'{rep.failures} CHECK(S) FAILED  ({rep.checked:,} comparisons)')
    return 1


if __name__ == '__main__':
    sys.exit(main())
