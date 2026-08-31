#!/usr/bin/env python3
"""Validate the map JSON exports against an INDEPENDENT recomputation.

This script does NOT import `src/school_quality` and does not read the notebook.
That independence is the whole point: it re-reads the source xlsx and re-derives
every per-year metric at every reference level, every view aggregate
(base / single_year / loo / last_k), composite_min, every rank and percentile,
every region aggregate and the colour-scale metadata from scratch, then checks
the JSON the app actually serves against it. A check that shared the pipeline's
helpers would only restate the pipeline.

The export it validates (spec §5):

  docs/data/schools-index.json                 identity for every school
  docs/data/scale.json                         colour-scale anchors per level
  docs/data/regions-{level}.json         × 3   voivodeship / powiat / gmina rows
  docs/data/powiat/{teryt4}-{metric}.json      the per-school views
  docs/geo/                                    boundary polygons, keyed by TERYT

A school's difference-based score exists at four REFERENCE LEVELS (national,
voivodeship, powiat, gmina) — the population whose per-year mean it is measured
against. `mean` and `median` are raw 0-100 aggregates with no reference
population at all (spec §4.2), so they are computed once, at the primary level,
and the shards carry those same numbers under all four level keys. Recomputing
a national reference and comparing it to a voivodeship-referenced cell is the
mistake this file is shaped to avoid.

The shards total about a gigabyte, so every check that reads them STREAMS one
file at a time and keeps only counters — none builds a whole-export map.

Checks
  A. Subject raw values:  JSON single_year of metric 'mean'/'median' == the
     mean/median read straight from the xlsx for that (school, subject, year).
  B. Aggregates:          recomputed score == JSON score for every
     (level, metric, subject, view) — single_year / loo / base / last_k +
     composite_min. A null-score cell must have no recomputed counterpart.
  C. Completeness:        the set of (level, metric, subject, view, param,
     school) cells in the JSON equals the recomputed set (nothing dropped or
     invented), counting the four level copies of mean/median separately.
  D. Index <-> shards:    every school in schools-index.json resolves to the
     shard its `powiat` names and appears there, and every shard school appears
     in the index. The two files are the only cross-references left once
     identity moved out of the shards.
  E. Ranks / percentiles: per view population, the stored rank/pct match
     rankdata over the recomputed scores (min-rank ties, average-rank
     percentile), up to a near-tie tolerance.
  F. Colour-scale metadata: recomputed sigma / centre / p1 / p99 /
     slider_ranges == scale.json, and the region files' own sigma / centre and
     suppression thresholds == the recomputation.
  G. Class spread:        bucketing base scores into A/B/C by ±0.33sigma (the
     map's colour rule) leaves no class empty.
  H. Identity + frontend contract: (rspo, year) unique in the source; the index
     school set == the source set; n_years == the source per-school year count;
     the parallel arrays are the same length; and the contract fields the app
     reads before anything else loads are populated.
  I. Region aggregates reconcile (§7.8): every region's n_schools, n_students,
     score, rank, pct and n_ranked recomputed from its own schools.
  J. Geometry and regions agree (§7.9): the TERYT key sets match exactly.
  K. No NaN reaches the JSON (§7.10): every emitted file re-parsed with a parser
     that refuses the bare NaN / Infinity tokens json.dumps is happy to write.
  L. Size budgets (§7.11): every emitted file gzipped and compared to §5.7.
  M. Percentiles gated on the SIBLING count (§7.5).
  N. Diff scores gated on the PARENT's school count (§7.6).
  O. Voivodeship codes and names are a bijection (§7.2), 16 of each.
  P. Region keys are stable across years (§7.4): the voivodeship and powiat keys
     must not move, the published teryt must be the school's newest source row,
     and a school whose gmina genuinely changes is reported by name rather than
     failed — that is a real event, not a fault.

M and N gate on two different populations, and conflating them is the easiest
way to break this map. Both derive their populations independently — the
siblings from the docs/geo key set, the parent's schools from the source rows —
never from the published `parent[]` / `n_schools[]` arrays, which would turn the
check into a restatement of the export.

Usage
  uv run python scripts/validate_export.py
  uv run python scripts/validate_export.py --data-dir data/egzamin-osmoklasisty \
      --docs-data docs/data --geo-dir docs/geo
Exit code 0 if everything passes, 1 otherwise.
"""

from __future__ import annotations

import argparse
import bisect
import gzip
import json
import math
import re
import sys
import unicodedata
from collections import Counter, defaultdict
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
Teryt = str  # a region key: 2 digits (voivodeship), 4 (powiat), 6 (gmina)
Subject = str  # 'polski' | 'matematyka' | 'angielski' | 'composite_min'
Metric = str  # 'mean' | 'median' | 'diff_mean' | 'unit_norm_diff_mean'
Level = str  # 'national' | 'voivodeship' | 'powiat' | 'gmina'
ViewKind = str  # 'base' | 'loo' | 'single_year' | 'last_k'
# A view is identified by (level, metric, subject, view_kind, param); param is
# None for base, else an int (the excluded/selected year, or k).
ViewKey = tuple[Level, Metric, Subject, ViewKind, 'int | None']
ViewScores = dict[ViewKey, dict[Rspo, float]]


class ScoreCell(TypedDict):
    """One exported value: the aggregate score, its rank (1 = best) and percentile."""

    score: float
    rank: int
    pct: float


CORE_SUBJECTS = ['polski', 'matematyka', 'angielski']
ALL_SUBJECTS = CORE_SUBJECTS + ['composite_min']
EXPORT_METRICS = ['mean', 'median', 'diff_mean', 'unit_norm_diff_mean']
# The metrics whose score is a distance from a reference population. Only these
# vary by reference level, and only these are ever suppressed.
REFERENCED_METRICS = ['diff_mean', 'unit_norm_diff_mean']
WEIGHTED_METRICS = set(REFERENCED_METRICS)  # the rest use a plain mean
YEAR_FILE_RE = re.compile(r'^\d{4}')

REFERENCE_LEVELS = ['national', 'voivodeship', 'powiat', 'gmina']
PRIMARY_REFERENCE_LEVEL = 'voivodeship'
# How many TERYT digits identify a region at each level; national is the whole
# country, so its key is the empty prefix.
REFERENCE_WIDTH = {'national': 0, 'voivodeship': 2, 'powiat': 4, 'gmina': 6}
# The three levels the map draws as polygons, and what each is measured against.
REGION_LEVELS = ['voivodeship', 'powiat', 'gmina']
PARENT_WIDTH = {'voivodeship': 0, 'powiat': 2, 'gmina': 4}
PARENT_REFERENCE_LEVEL = {'voivodeship': 'national', 'powiat': 'voivodeship',
                          'gmina': 'powiat'}

# Spec §4.3. Restated here rather than imported: the thresholds are half of what
# checks M and N test, so reading them from the pipeline would make the test
# agree with itself. Check F compares these against the published metadata.
MIN_REFERENCE_N = 5
MIN_PERCENTILE_N = 8

# Poland has had 16 voivodeships since 1999. Check O pins the count so a source
# edition that merged or renamed one says so instead of quietly reshaping a
# reference population.
EXPECTED_VOIVODESHIPS = 16

# Spec §5.7, in gzipped KiB. Budgets are a gate, not guidance.
FILE_BUDGETS_KB = {
    'regions-voivodeship.json': 50,
    'regions-powiat.json': 300,
    'regions-gmina.json': 1500,
    'schools-index.json': 550,
    'scale.json': 20,
}
SHARD_BUDGET_KB = 1024
GEOMETRY_BUDGET_KB = 250
# Level 6 is what the measurements in §5.7 were taken at, and what a static host
# serves; level 9 would flatter the files by a few per cent.
GZIP_LEVEL = 6

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
TERYT_COL = 'kod teryt gminy'


def source_level(metric: Metric, level: Level) -> Level:
    """Which reference level a metric's score is actually computed at.

    `mean` and `median` have no reference population, so the export computes them
    once at the primary level and repeats them under all four level keys. Asking
    the recomputation for ('national', 'mean', ...) would find nothing.
    """
    return PRIMARY_REFERENCE_LEVEL if metric not in WEIGHTED_METRICS else level


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


def normalise_teryt(value) -> str:
    """A 7-character zero-padded TERYT gmina code, or exit with the offending value.

    A spreadsheet round-trip eats the leading zero of voivodeships 02/04/06/08, so
    padding is load-bearing. Anything not 6 or 7 digits is a different code
    entirely and padding it would invent a region.
    """
    text = str(value).strip().removesuffix('.0')
    if not text.isdigit() or len(text) not in (6, 7):
        sys.exit(f'not a TERYT gmina code: {value!r}')
    return text.zfill(7)


def read_clean_rows(data_dir: Path) -> list[dict]:
    """Read the source xlsx and return one dict per surviving (school, year) row:
    {'rspo', 'year', 'teryt', 'wojewodztwo', <subject>: {'n', 'mean', 'median'}}.

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
    meta_needed = [('meta', 'rspo'), ('meta', 'wojewodztwo - nazwa'), ('meta', TERYT_COL)]
    per_file_frames = []
    for path in files:
        year = int(YEAR_FILE_RE.match(path.name).group(0))
        try:
            sheet = pd.read_excel(path, sheet_name='SAS', header=[0, 1])
        except ValueError as exc:
            sys.exit(f"Cannot read sheet 'SAS' in {path.name}: {exc}")
        sheet.columns = normalize_columns(sheet.columns)
        missing = [c for c in meta_needed + needed if c not in sheet.columns]
        if missing:
            sys.exit(f'{path.name}: expected columns not found after normalisation: {missing}')

        column_data = {
            'rspo': pd.to_numeric(sheet[('meta', 'rspo')], errors='coerce'),
            'wojewodztwo': sheet[('meta', 'wojewodztwo - nazwa')].astype(str).str.strip(),
            'teryt': sheet[('meta', TERYT_COL)].map(normalise_teryt),
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
    df = df[keep]

    rows = []
    for record in df.to_dict('records'):
        row = {
            'rspo': int(record['rspo']),
            'year': int(record['year']),
            'teryt': record['teryt'],
            'wojewodztwo': record['wojewodztwo'],
        }
        for subject in CORE_SUBJECTS:
            row[subject] = {
                'n': float(record[f'n_{subject}']),
                'mean': float(record[f'mean_{subject}']),
                'median': float(record[f'median_{subject}']),
            }
        rows.append(row)
    return rows


# ── independent metric / view / rank recomputation ──────────────────────────


def compute_reference_stats(
    rows: list[dict], level: Level
) -> tuple[dict[tuple[Year, Teryt], dict[Subject, float]], dict[tuple[Year, Teryt], int]]:
    """(year, region) -> {subject: mean of school means}, and (year, region) -> n.

    The mean is the per-year reference that diff_mean / unit_norm_diff_mean
    subtract off; n is the size of that reference population, which spec §4.3
    gates a school's difference score on.
    """
    width = REFERENCE_WIDTH[level]
    grouped: dict[tuple, dict[Subject, list]] = defaultdict(
        lambda: {subject: [] for subject in CORE_SUBJECTS}
    )
    for row in rows:
        bucket = grouped[(row['year'], row['teryt'][:width])]
        for subject in CORE_SUBJECTS:
            bucket[subject].append(row[subject]['mean'])
    reference_mean, reference_n = {}, {}
    for key, bucket in grouped.items():
        reference_n[key] = len(bucket['polski'])
        reference_mean[key] = {
            subject: math.fsum(values) / len(values) for subject, values in bucket.items()
        }
    return reference_mean, reference_n


def compute_per_year_values(
    rows: list[dict], level: Level
) -> dict[tuple[Rspo, Subject], dict[Year, dict[str, float]]]:
    """(rspo, subject) -> year -> {'n', 'mean', 'median', 'diff_mean',
    'unit_norm_diff_mean', 'reference_n'} for valid rows (n > 0), with the
    difference metrics measured against `level`'s per-year reference.
    unit_norm uses the signed ceiling/floor normalisation to [-1, +1]."""
    reference_mean, reference_n = compute_reference_stats(rows, level)
    width = REFERENCE_WIDTH[level]
    per_year_by_school_subject: dict[tuple, dict[Year, dict]] = defaultdict(dict)
    for row in rows:
        year = row['year']
        group = (year, row['teryt'][:width])
        references, population = reference_mean[group], reference_n[group]
        for subject in CORE_SUBJECTS:
            n = row[subject]['n']
            if not n > 0:  # NaN-safe: NaN > 0 is False
                continue
            mean = row[subject]['mean']
            reference = references[subject]
            diff_mean = mean - reference
            if diff_mean >= 0:
                denom = (100 - reference) if (100 - reference) > 0 else 1
            else:
                denom = reference if reference > 0 else 1
            per_year_by_school_subject[(row['rspo'], subject)][year] = {
                'n': n,
                'mean': mean,
                'median': row[subject]['median'],
                'diff_mean': diff_mean,
                'unit_norm_diff_mean': diff_mean / denom,
                'reference_n': population,
            }
    return per_year_by_school_subject


def aggregate_across_years(
    metric: Metric, year_to_values: dict[Year, dict[str, float]], years_used
) -> float:
    """Aggregate a metric across `years_used` for one school. Weighted by the
    student count for diff_mean / unit_norm_diff_mean, a plain mean otherwise.
    Zero-weight years are ignored in the weighted case; an empty selection
    returns NaN. Kept in plain Python: the arrays are at most six long, where
    numpy's per-call overhead dominates and this runs several million times."""
    values = [year_to_values[year][metric] for year in years_used]
    if not values:
        return math.nan
    if metric in WEIGHTED_METRICS:
        weights = [year_to_values[year]['n'] for year in years_used]
        total = math.fsum(weight for weight in weights if weight > 0)
        if not total:
            return math.nan
        weighted = math.fsum(v * w for v, w in zip(values, weights) if w > 0)
        return weighted / total
    return math.fsum(values) / len(values)


def compute_view_scores(rows: list[dict]) -> ViewScores:
    """(level, metric, subject, view_kind, param) -> {rspo: score}.

    Each school's views use only its own years: base (all), loo (one excluded,
    >= 2 years), single_year (each), last_k (most recent k, k = 2..n-1).
    composite_min = min over the 3 core subjects, for the schools present in all
    three for that view. A difference score whose smallest per-year reference
    population is under MIN_REFERENCE_N is withheld (spec §4.3), which is why the
    gmina level carries far fewer cells than the national one.

    mean/median are computed at the primary level only — spec §4.2; see
    source_level().
    """
    view_key_to_rspo_scores: dict[tuple, dict[Rspo, float]] = defaultdict(dict)

    for level in REFERENCE_LEVELS:
        per_year_by_school_subject = compute_per_year_values(rows, level)
        metrics = EXPORT_METRICS if level == PRIMARY_REFERENCE_LEVEL else REFERENCED_METRICS
        for metric in metrics:
            for (rspo, subject), year_to_values in per_year_by_school_subject.items():
                years = sorted(year_to_values)
                n_years = len(years)

                def store(view_kind, param, years_used):
                    score = aggregate_across_years(metric, year_to_values, years_used)
                    if math.isnan(score):
                        return
                    if metric in REFERENCED_METRICS:
                        smallest = min(year_to_values[y]['reference_n'] for y in years_used)
                        if smallest < MIN_REFERENCE_N:
                            return
                    key = (level, metric, subject, view_kind, param)
                    view_key_to_rspo_scores[key][rspo] = score

                store('base', None, years)
                if n_years >= 2:
                    for excluded in years:
                        store('loo', excluded, [y for y in years if y != excluded])
                for year in years:
                    store('single_year', year, [year])
                for k in range(2, n_years):
                    store('last_k', k, years[-k:])

            view_params = {
                (view_kind, param)
                for (lvl, met, sub, view_kind, param) in view_key_to_rspo_scores
                if lvl == level and met == metric and sub in CORE_SUBJECTS
            }
            for view_kind, param in view_params:
                per_subject = {
                    subject: view_key_to_rspo_scores.get(
                        (level, metric, subject, view_kind, param), {}
                    )
                    for subject in CORE_SUBJECTS
                }
                shared_rspos = (
                    set(per_subject['polski'])
                    & set(per_subject['matematyka'])
                    & set(per_subject['angielski'])
                )
                composite = view_key_to_rspo_scores[
                    (level, metric, 'composite_min', view_kind, param)
                ]
                for rspo in shared_rspos:
                    composite[rspo] = min(per_subject[s][rspo] for s in CORE_SUBJECTS)
    return dict(view_key_to_rspo_scores)


def compute_students_per_school(rows: list[dict]) -> dict[Rspo, int]:
    """rspo -> the cohort size the region aggregates weight that school by.

    The export stores, per base view, the MEDIAN students per year of the subject
    behind that view, and weights a region by the largest of them — so this is
    the maximum over the three core subjects of that school's median cohort.
    """
    per_school: dict[Rspo, dict[Subject, list]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        for subject in CORE_SUBJECTS:
            n = row[subject]['n']
            if n > 0:
                per_school[row['rspo']][subject].append(n)
    return {
        rspo: max(int(round(float(np.median(counts)))) for counts in by_subject.values())
        for rspo, by_subject in per_school.items()
    }


def latest_teryt_per_school(rows: list[dict]) -> dict[Rspo, str]:
    """rspo -> its TERYT in the most recent year it appears, which is the one the
    export files it under. A school that changes gmina moves with its newest row."""
    latest: dict[Rspo, tuple[Year, str]] = {}
    for row in rows:
        known = latest.get(row['rspo'])
        if known is None or row['year'] > known[0]:
            latest[row['rspo']] = (row['year'], row['teryt'])
    return {rspo: teryt for rspo, (_, teryt) in latest.items()}


def aggregate_region(scores, weights, weighted: bool):
    """One region's score from its schools', or None if none of them has one.

    Student-weighted for the difference metrics and arithmetic for mean/median,
    mirroring the across-years rule (spec §5.1). A region whose schools all
    report zero pupils falls back to the arithmetic mean rather than dividing by
    zero.
    """
    pairs = [(s, w) for s, w in zip(scores, weights) if s is not None]
    if not pairs:
        return None
    if not weighted:
        return math.fsum(s for s, _ in pairs) / len(pairs)
    total = math.fsum(w for _, w in pairs)
    if not total:
        return math.fsum(s for s, _ in pairs) / len(pairs)
    return math.fsum(s * w for s, w in pairs) / total


def rank_and_percentile(scores: list) -> tuple[list, list]:
    """Ranks and percentiles over the entries that have a score.

    Min-rank with 1 = highest score, and percentile = ascending average rank / n,
    so 100 is best — the same convention as the school path. Entries with no
    score come back None in both lists and are not counted in n.
    """
    present = [(i, s) for i, s in enumerate(scores) if s is not None]
    ranks: list = [None] * len(scores)
    percentiles: list = [None] * len(scores)
    if not present:
        return ranks, percentiles
    values = np.array([s for _, s in present], dtype=float)
    n = len(values)
    computed_ranks = stats.rankdata(-values, method='min').astype(int)
    computed_percentiles = stats.rankdata(values, method='average') / n * 100
    for (i, _), rank, percentile in zip(present, computed_ranks, computed_percentiles):
        ranks[i] = int(rank)
        percentiles[i] = round(float(percentile), 1)
    return ranks, percentiles


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


# ── region recomputation (spec §5.1, §5.2) ──────────────────────────────────


class RecomputedLevel(TypedDict):
    """One region level, rebuilt from the geometry key set and the source rows."""

    regions: list[Teryt]
    parent: list[Teryt]
    members: dict[Teryt, list[Rspo]]
    siblings: dict[Teryt, int]
    parent_schools: dict[Teryt, int]
    n_schools: list[int]
    n_students: list[int]
    score: dict[Metric, dict[Subject, list]]
    rank: dict[Metric, dict[Subject, list]]
    pct: dict[Metric, dict[Subject, list]]
    n_ranked: dict[Metric, dict[Subject, int]]
    has_scored_member: dict[Metric, dict[Subject, list[bool]]]


def compute_region_level(
    level: Level,
    geometry: set[Teryt],
    teryt_by_rspo: dict[Rspo, str],
    students_by_rspo: dict[Rspo, int],
    view_key_to_rspo_scores: ViewScores,
) -> RecomputedLevel:
    """Rebuild one regions-{level}.json from the geometry and the source rows.

    Row identity comes from the geometry (spec §5.1), so a region with no scored
    school is a real row with n_schools = 0 rather than a hole. A region's score
    is measured against the level ABOVE it, so its schools' scores are read at
    PARENT_REFERENCE_LEVEL — taking primary-level scores for every level would
    put all 16 voivodeships at ~0 by construction.
    """
    width = REFERENCE_WIDTH[level]
    parent_width = PARENT_WIDTH[level]
    regions = sorted(geometry)
    parents = [region[:parent_width] for region in regions]

    members: dict[Teryt, list[Rspo]] = {region: [] for region in regions}
    for rspo, teryt in teryt_by_rspo.items():
        members.setdefault(teryt[:width], []).append(rspo)

    siblings: Counter = Counter(parents)
    parent_schools: Counter = Counter()
    for region, parent in zip(regions, parents):
        parent_schools[parent] += len(members[region])

    result: RecomputedLevel = {
        'regions': regions,
        'parent': parents,
        'members': members,
        'siblings': dict(siblings),
        'parent_schools': dict(parent_schools),
        'n_schools': [len(members[region]) for region in regions],
        'n_students': [
            sum(students_by_rspo.get(rspo, 0) for rspo in members[region]) for region in regions
        ],
        'score': {}, 'rank': {}, 'pct': {}, 'n_ranked': {}, 'has_scored_member': {},
    }

    for metric in EXPORT_METRICS:
        for field in ('score', 'rank', 'pct', 'n_ranked', 'has_scored_member'):
            result[field][metric] = {}
        weighted = metric in WEIGHTED_METRICS
        for subject in ALL_SUBJECTS:
            reference = source_level(metric, PARENT_REFERENCE_LEVEL[level])
            base_scores = view_key_to_rspo_scores.get(
                (reference, metric, subject, 'base', None), {}
            )
            scores: list = []
            scored_member: list[bool] = []
            for region, parent in zip(regions, parents):
                rspos = members[region]
                raw = aggregate_region(
                    [base_scores.get(rspo) for rspo in rspos],
                    [students_by_rspo.get(rspo, 0) for rspo in rspos],
                    weighted,
                )
                scored_member.append(raw is not None)
                publishable = not weighted or (
                    siblings[parent] > 1 and parent_schools[parent] >= MIN_REFERENCE_N
                )
                scores.append(None if raw is None or not publishable else round(raw, 4))

            ranks, _ = rank_and_percentile(scores)
            percentiles: list = [None] * len(regions)
            by_parent: dict[Teryt, list[int]] = defaultdict(list)
            for index, parent in enumerate(parents):
                by_parent[parent].append(index)
            for parent, indexes in by_parent.items():
                if siblings[parent] < MIN_PERCENTILE_N:
                    continue
                _, group = rank_and_percentile([scores[i] for i in indexes])
                for index, percentile in zip(indexes, group):
                    percentiles[index] = percentile

            result['score'][metric][subject] = scores
            result['rank'][metric][subject] = ranks
            result['pct'][metric][subject] = percentiles
            result['n_ranked'][metric][subject] = sum(1 for s in scores if s is not None)
            result['has_scored_member'][metric][subject] = scored_member
    return result


# ── JSON loading (streamed; the shards do not fit in memory together) ────────


def load_json_exports(docs_data: Path) -> tuple[dict, dict, dict[Level, dict]]:
    """Load the three small served files. Returns (scale, index_columns, regions).

    The powiat shards are NOT loaded here: they total about a gigabyte, and every
    check that needs them streams instead (see iter_json_views).
    """
    def read(path: Path) -> dict:
        if not path.exists():
            sys.exit(f'Missing {path}')
        return json.loads(path.read_text(encoding='utf-8'))

    scale = read(docs_data / 'scale.json')
    index = read(docs_data / 'schools-index.json')
    regions = {level: read(docs_data / f'regions-{level}.json') for level in REGION_LEVELS}
    shard_dir = docs_data / 'powiat'
    if not shard_dir.is_dir():
        sys.exit(f'Missing {shard_dir}')
    return scale, index, regions


def shard_paths(docs_data: Path, metrics: list[Metric] | None = None) -> list[Path]:
    """The per-powiat detail files, optionally narrowed to some metrics."""
    wanted = set(metrics or EXPORT_METRICS)
    return sorted(
        path
        for path in (docs_data / 'powiat').glob('*.json')
        if path.stem.split('-', 1)[-1] in wanted
    )


def iter_shards(
    docs_data: Path, metrics: list[Metric] | None = None
) -> Iterator[tuple[Teryt, Metric, dict]]:
    """Yield (powiat teryt, metric, payload) one shard at a time, so at most one
    file's worth of parsed JSON is alive."""
    for path in shard_paths(docs_data, metrics):
        teryt, metric = path.stem.split('-', 1)
        yield teryt, metric, json.loads(path.read_text(encoding='utf-8'))


def iter_json_views(
    docs_data: Path, metrics: list[Metric] | None = None
) -> Iterator[tuple[Level, Metric, Subject, ViewKind, int | None, Rspo, ScoreCell]]:
    """Yield (level, metric, subject, view_kind, param, rspo, cell) for every score
    cell in the shards (base param is None, others are ints).

    Cells with a null score are yielded too: they are the export's placeholder for
    "this school has no record at this reference level", and a check that skipped
    them could not tell a legitimate suppression from a lost row.
    """
    for _, metric, payload in iter_shards(docs_data, metrics):
        for rspo_text, school in payload['schools'].items():
            rspo = int(rspo_text)
            for level, by_subject in school.items():
                for subject, views in by_subject.items():
                    base = views.get('base')
                    if base is not None:
                        yield level, metric, subject, 'base', None, rspo, base
                    for view_kind in ('single_year', 'loo', 'last_k'):
                        for param, cell in (views.get(view_kind) or {}).items():
                            yield level, metric, subject, view_kind, int(param), rspo, cell


def geometry_keys(geo_dir: Path, level: Level) -> set[Teryt]:
    """The TERYT keys of every polygon at `level`, from the PRG JPT_KOD_JE property."""
    directory = {'powiat': 'woj', 'gmina': 'pow'}.get(level)
    files = [geo_dir / 'kraj.json'] if directory is None else sorted(
        (geo_dir / directory).glob('*.json')
    )
    if not files:
        sys.exit(f'No boundary files for level {level} under {geo_dir}')
    width = REFERENCE_WIDTH[level]
    keys = set()
    for path in files:
        for feature in json.loads(path.read_text(encoding='utf-8'))['features']:
            keys.add(feature['properties']['JPT_KOD_JE'][:width])
    return keys


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

    def note(self, msg: str):
        print(f'        {msg}')

    def fail(self, msg: str, examples=None):
        self.failures += 1
        print(f'  FAIL  {msg}')
        for example in (examples or [])[:5]:
            print(f'          {example}')


def within_tolerance(a, b, tol: float) -> bool:
    """True if two numbers are finite and within `tol` of each other."""
    return a is not None and b is not None and abs(a - b) <= tol


def values_disagree(published, recomputed, tol: float) -> bool:
    """True if a published value differs from its recomputation, nulls included.

    `None` is a value here, not a missing one: it is how the export says
    "suppressed", so publishing a number where the rules call for `None` (or the
    reverse) is exactly the failure the region checks are looking for.
    """
    if (published is None) != (recomputed is None):
        return True
    return published is not None and not within_tolerance(published, recomputed, tol)


# ── checks ───────────────────────────────────────────────────────────────────


def check_subjects_vs_xlsx(rows, docs_data: Path, rep: Report):
    """A — the single-year scores of the 'mean'/'median' metrics must equal the
    mean/median read straight from the xlsx (ties the JSON to the raw source).
    Checked at all four level keys, which for these two metrics hold copies."""
    rep.section('A. Subject raw values vs xlsx (single_year of mean/median)')
    rspo_year_to_row = {(row['rspo'], row['year']): row for row in rows}
    stats_by_metric = defaultdict(lambda: [0, 0.0])  # metric -> [checked, max_diff]
    mismatches = defaultdict(list)
    for level, metric, subject, view_kind, param, rspo, cell in iter_json_views(
        docs_data, metrics=['mean', 'median']
    ):
        if view_kind != 'single_year' or subject not in CORE_SUBJECTS:
            continue
        row = rspo_year_to_row.get((rspo, param))
        if row is None:
            mismatches[metric].append(f'rspo={rspo} {subject} {param}: no such row in the xlsx')
            continue
        expected = row[subject][metric]
        stats_by_metric[metric][0] += 1
        stats_by_metric[metric][1] = max(stats_by_metric[metric][1], abs(cell['score'] - expected))
        if not within_tolerance(cell['score'], expected, SCORE_TOL):
            mismatches[metric].append(
                f'{level}/{subject} rspo={rspo} {param}: '
                f'json={cell["score"]} xlsx={expected}'
            )
    for metric, (checked, max_diff) in sorted(stats_by_metric.items()):
        rep.checked += checked
        if mismatches[metric]:
            rep.fail(
                f'metric={metric}: {len(mismatches[metric])}/{checked} single-year values '
                f'differ from xlsx',
                mismatches[metric],
            )
        else:
            rep.ok(
                f'metric={metric}: all {checked:,} single-year values match the xlsx '
                f'(max delta {max_diff:.2e})'
            )


def check_aggregates(view_key_to_rspo_scores: ViewScores, docs_data: Path, rep: Report):
    """B — every JSON aggregate score (all levels x metrics x subjects x views)
    must equal the independent recomputation, and a null-score cell must have no
    recomputed counterpart. Reports the largest deviation per view kind."""
    rep.section('B. Aggregates recomputed from xlsx vs JSON (all views, all levels)')
    kind_to_stats = defaultdict(lambda: [0, 0, 0.0])  # view_kind -> [checked, failed, max_diff]
    examples = defaultdict(list)
    for level, metric, subject, view_kind, param, rspo, cell in iter_json_views(docs_data):
        key = (source_level(metric, level), metric, subject, view_kind, param)
        recomputed = view_key_to_rspo_scores.get(key, {}).get(rspo)
        kind_to_stats[view_kind][0] += 1
        if cell['score'] is None:
            if recomputed is not None:
                kind_to_stats[view_kind][1] += 1
                examples[view_kind].append(
                    f'{level}/{metric}/{subject}/{view_kind}/{param} rspo={rspo}: '
                    f'json=null but recomputed={recomputed:.6f}'
                )
            continue
        if recomputed is None:
            kind_to_stats[view_kind][1] += 1
            examples[view_kind].append(
                f'{level}/{metric}/{subject}/{view_kind}/{param} rspo={rspo}: '
                f'missing in recompute'
            )
            continue
        diff = abs(cell['score'] - recomputed)
        kind_to_stats[view_kind][2] = max(kind_to_stats[view_kind][2], diff)
        if diff > SCORE_TOL:
            kind_to_stats[view_kind][1] += 1
            examples[view_kind].append(
                f'{level}/{metric}/{subject}/{view_kind}/{param} rspo={rspo}: '
                f'json={cell["score"]} recomp={recomputed:.6f}'
            )
    for view_kind, (checked, failed, max_diff) in sorted(kind_to_stats.items()):
        rep.checked += checked
        if failed:
            rep.fail(
                f'{view_kind}: {failed}/{checked:,} scores differ (max delta {max_diff:.2e})',
                examples[view_kind],
            )
        else:
            rep.ok(
                f'{view_kind}: all {checked:,} aggregate scores match (max delta {max_diff:.2e})'
            )


def check_completeness(view_key_to_rspo_scores: ViewScores, docs_data: Path, rep: Report):
    """C — the JSON and the recomputation must contain exactly the same score
    cells. Counted per view key rather than as two sets of tuples: the export has
    ~11.5M cells and materialising both sides would need gigabytes."""
    rep.section('C. Completeness: JSON cell set == recomputed cell set')
    matched: Counter = Counter()
    problems: list[str] = []
    json_only = 0
    for level, metric, subject, view_kind, param, rspo, cell in iter_json_views(docs_data):
        json_key = (level, metric, subject, view_kind, param)
        recomputed = view_key_to_rspo_scores.get(
            (source_level(metric, level), metric, subject, view_kind, param), {}
        )
        if cell['score'] is None:
            continue  # the export's "no record at this level" placeholder
        if rspo not in recomputed:
            json_only += 1
            if len(problems) < 5:
                problems.append(f'in JSON only: {json_key} rspo={rspo}')
            continue
        matched[json_key] += 1

    total = 0
    recompute_only = 0
    for level in REFERENCE_LEVELS:
        for metric in EXPORT_METRICS:
            for subject in ALL_SUBJECTS:
                for key, scores in view_key_to_rspo_scores.items():
                    if key[:3] != (source_level(metric, level), metric, subject):
                        continue
                    json_key = (level, metric, subject, key[3], key[4])
                    total += len(scores)
                    deficit = len(scores) - matched[json_key]
                    if deficit:
                        recompute_only += deficit
                        if len(problems) < 5:
                            problems.append(
                                f'in recompute only: {json_key} — {deficit} of '
                                f'{len(scores)} schools absent from the JSON'
                            )
    rep.checked += total + json_only
    if json_only or recompute_only:
        rep.fail(f'{json_only} JSON-only + {recompute_only} recompute-only cells', problems)
    else:
        rep.ok(f'identical cell sets ({total:,} score cells)')


def check_index_shard_join(index: dict, docs_data: Path, rep: Report):
    """D — schools-index.json and the shards describe one population, and the
    index's `powiat` column is the only thing that tells the app which shard to
    fetch. Identity no longer lives in the shards, so this join is the sole
    cross-reference between the two files: a school in the index whose shard has
    no such row is a dead deep link, and a shard row with no index entry is a
    school with no name."""
    rep.section('D. schools-index.json <-> the powiat shards')
    index_by_powiat: dict[Teryt, set[Rspo]] = defaultdict(set)
    for rspo, powiat in zip(index['rspo'], index['powiat']):
        index_by_powiat[powiat].add(int(rspo))

    problems: list[str] = []
    for powiat in sorted(index_by_powiat):
        for metric in EXPORT_METRICS:
            if not (docs_data / 'powiat' / f'{powiat}-{metric}.json').exists():
                problems.append(f'{len(index_by_powiat[powiat])} schools reference the '
                                f'missing shard {powiat}-{metric}.json')

    checked = 0
    for powiat, metric, payload in iter_shards(docs_data):
        shard_rspos = {int(rspo) for rspo in payload['schools']}
        checked += len(shard_rspos)
        metadata = payload.get('metadata', {})
        if metadata.get('metric') != metric or metadata.get('powiat') != powiat:
            problems.append(f'{powiat}-{metric}.json: metadata says '
                            f'{metadata.get("powiat")!r}/{metadata.get("metric")!r}')
        expected = index_by_powiat.get(powiat, set())
        if shard_rspos != expected:
            problems.append(
                f'{powiat}-{metric}.json: {len(shard_rspos - expected)} schools not in the '
                f'index {sorted(shard_rspos - expected)[:3]}, '
                f'{len(expected - shard_rspos)} index schools not in the shard '
                f'{sorted(expected - shard_rspos)[:3]}'
            )
    rep.checked += checked
    if problems:
        rep.fail(f'{len(problems)} index/shard disagreements', problems)
    else:
        rep.ok(f'{len(index["rspo"]):,} schools resolve to their shard in all '
               f'{len(EXPORT_METRICS)} metric files ({checked:,} shard rows)')


def check_ranks(view_key_to_rspo_scores: ViewScores, docs_data: Path, rep: Report):
    """E — per view population, the stored rank/pct must match rankdata over the
    recomputed (full-precision) scores, accepting any order among near-ties (see
    acceptable_rank_range). The population is checked by counting rather than by
    building sets, for the same memory reason as check C."""
    rep.section('E. Ranks/percentiles match rankdata over the recomputed scores')
    scores_ascending = {
        key: sorted(scores.values()) for key, scores in view_key_to_rspo_scores.items()
    }
    seen: Counter = Counter()
    rank_fail = pct_fail = total = ambiguous = missing = 0
    examples: list[str] = []
    for level, metric, subject, view_kind, param, rspo, cell in iter_json_views(docs_data):
        if cell['score'] is None:
            continue
        key = (source_level(metric, level), metric, subject, view_kind, param)
        score = view_key_to_rspo_scores.get(key, {}).get(rspo)
        if score is None:
            missing += 1
            continue  # already reported by check B; nothing to rank against
        seen[(level, metric, subject, view_kind, param)] += 1
        total += 1
        rank_lo, rank_hi, pct_lo, pct_hi = acceptable_rank_range(score, scores_ascending[key])
        if rank_lo != rank_hi:
            ambiguous += 1
        if not (rank_lo <= cell['rank'] <= rank_hi):
            rank_fail += 1
            if len(examples) < 5:
                examples.append(
                    f'{level}/{metric}/{subject}/{view_kind}/{param} rspo={rspo}: '
                    f'json_rank={cell["rank"]} not in [{rank_lo},{rank_hi}]'
                )
            continue
        if not (pct_lo - PCT_TOL <= cell['pct'] <= pct_hi + PCT_TOL):
            pct_fail += 1
            if len(examples) < 5:
                examples.append(
                    f'{level}/{metric}/{subject}/{view_kind}/{param} rspo={rspo}: '
                    f'json_pct={cell["pct"]} out of [{pct_lo:.1f},{pct_hi:.1f}]'
                )

    pop_fail = 0
    for level in REFERENCE_LEVELS:
        for key, scores in view_key_to_rspo_scores.items():
            if key[0] != source_level(key[1], level):
                continue
            json_key = (level, key[1], key[2], key[3], key[4])
            if seen[json_key] != len(scores):
                pop_fail += 1
                if len(examples) < 5:
                    examples.append(
                        f'{json_key}: population differs '
                        f'(json {seen[json_key]}, recomputed {len(scores)})'
                    )
    rep.checked += total
    if pop_fail or rank_fail or pct_fail or missing:
        rep.fail(
            f'{pop_fail} population + {rank_fail} rank + {pct_fail} pct mismatches '
            f'({missing} cells unrankable) of {total:,} ranked',
            examples,
        )
    else:
        rep.ok(
            f'all {total:,} (rank, pct) values consistent with the scores '
            f'({ambiguous:,} within near-tie tolerance)'
        )


def check_metadata(scale, regions, view_key_to_rspo_scores, recomputed_regions, rep: Report):
    """F — recompute the colour-scale parameters and compare them to scale.json
    and to the region files' own metadata. sigma = std (sample) of base scores at
    that reference level; centre = mean for mean/median and composite_min, 0 for
    the diff metrics; p1/p99 from the ROUNDED base scores by position, and
    slider_ranges (min/max/p1/p99) from the unrounded composite_min distribution.
    These drive the map's colours and value filter, so a wrong one would
    mis-colour the map silently."""
    rep.section('F. Colour-scale metadata (scale.json + the region files)')
    school_scale = scale['school']
    slider_ranges = scale['metadata'].get('slider_ranges', {})
    mismatches, n = [], 0

    for level in REFERENCE_LEVELS:
        for metric in EXPORT_METRICS:
            reference = source_level(metric, level)
            published_metric = school_scale.get(level, {}).get(metric, {})
            for subject in ALL_SUBJECTS:
                base = view_key_to_rspo_scores.get((reference, metric, subject, 'base', None), {})
                values = np.array(list(base.values()))
                if len(values) < 2:
                    continue
                n += 1
                published = published_metric.get(subject, {})
                expected_sigma = round(float(values.std(ddof=1)), 4)
                expected_centre = (
                    round(float(values.mean()), 4)
                    if subject == 'composite_min' or metric not in WEIGHTED_METRICS
                    else 0.0
                )
                # p1/p99 are positional over the PUBLISHED (4 dp) scores, not
                # quantiles of the raw ones — reproduce the export's own rule.
                rounded = sorted(round(value, 4) for value in base.values())
                expected_p1 = rounded[max(0, int(0.01 * len(rounded)) - 1)]
                expected_p99 = rounded[min(len(rounded) - 1, int(0.99 * len(rounded)))]
                for field, expected in (
                    ('sigma', expected_sigma),
                    ('sigma_centre', expected_centre),
                    ('p1', expected_p1),
                    ('p99', expected_p99),
                ):
                    if not within_tolerance(published.get(field), expected, SCORE_TOL):
                        mismatches.append(
                            f'scale {level}/{metric}/{subject}.{field}: '
                            f'json={published.get(field)} recomp={expected}'
                        )

        for metric in EXPORT_METRICS:
            base = view_key_to_rspo_scores.get(
                (source_level(metric, level), metric, 'composite_min', 'base', None), {}
            )
            values = np.array(list(base.values()))
            if len(values) == 0 or metric not in slider_ranges.get(level, {}):
                continue
            n += 1
            expected = {
                'min': round(float(values.min()), 4),
                'max': round(float(values.max()), 4),
                'p1': round(float(np.quantile(values, 0.01)), 4),
                'p99': round(float(np.quantile(values, 0.99)), 4),
            }
            published = slider_ranges[level][metric]
            for field, value in expected.items():
                if not within_tolerance(published.get(field), value, SCORE_TOL):
                    mismatches.append(
                        f'slider {level}.{metric}.{field}: '
                        f'json={published.get(field)} recomp={value}'
                    )

    for level in REGION_LEVELS:
        metadata = regions[level]['metadata']
        if metadata.get('min_reference_n') != MIN_REFERENCE_N:
            mismatches.append(f'regions-{level} min_reference_n='
                              f'{metadata.get("min_reference_n")} != {MIN_REFERENCE_N}')
        if metadata.get('min_percentile_n') != MIN_PERCENTILE_N:
            mismatches.append(f'regions-{level} min_percentile_n='
                              f'{metadata.get("min_percentile_n")} != {MIN_PERCENTILE_N}')
        for metric in EXPORT_METRICS:
            for subject in ALL_SUBJECTS:
                n += 1
                values = [
                    v for v in recomputed_regions[level]['score'][metric][subject]
                    if v is not None
                ]
                deviation = float(np.std(values, ddof=1)) if len(values) > 1 else 0.0
                expected_sigma = round(0.0 if math.isnan(deviation) else deviation, 4)
                expected_centre = round(float(np.mean(values)), 4) if values else 0.0
                for field, expected in (('sigma', expected_sigma),
                                        ('sigma_centre', expected_centre)):
                    published = metadata[field][metric][subject]
                    if not within_tolerance(published, expected, SCORE_TOL):
                        mismatches.append(
                            f'regions-{level} {field} {metric}/{subject}: '
                            f'json={published} recomp={expected}'
                        )

    rep.checked += n
    if mismatches:
        rep.fail(f'{len(mismatches)} metadata mismatches', mismatches)
    else:
        rep.ok(f'sigma, centre, p1/p99, slider_ranges and the suppression thresholds '
               f'match the recomputation ({n} groups)')


def check_class_spread(scale, docs_data: Path, rep: Report):
    """G — bucket the published base scores into A / B / C by the map's +-0.33
    sigma rule and flag any EMPTY class: a colour the map can never show usually
    means a degenerate centre/sigma."""
    rep.section('G. Class A/B/C spread is non-degenerate (base scores, per level)')
    counts: dict[tuple, Counter] = defaultdict(Counter)
    for level, metric, subject, view_kind, _, _, cell in iter_json_views(docs_data):
        if view_kind != 'base' or cell['score'] is None:
            continue
        anchors = scale['school'].get(level, {}).get(metric, {}).get(subject)
        if not anchors or not anchors['sigma']:
            continue
        score, sigma, centre = cell['score'], anchors['sigma'], anchors['sigma_centre']
        if score > centre + CLASS_BOUND * sigma:
            counts[(level, metric, subject)]['A'] += 1
        elif score < centre - CLASS_BOUND * sigma:
            counts[(level, metric, subject)]['C'] += 1
        else:
            counts[(level, metric, subject)]['B'] += 1

    empty = []
    for level in REFERENCE_LEVELS:
        for metric in EXPORT_METRICS:
            for subject in ALL_SUBJECTS:
                anchors = scale['school'].get(level, {}).get(metric, {}).get(subject)
                if not anchors or not anchors['sigma']:
                    continue
                group = counts[(level, metric, subject)]
                for school_class in ('A', 'B', 'C'):
                    if not group[school_class]:
                        empty.append(f'{level}/{metric}/{subject}: class {school_class} '
                                     f'empty ({dict(group)})')
    rep.checked += len(counts)
    if empty:
        rep.fail(f'{len(empty)} (level, metric, subject) groups have an empty class', empty)
    else:
        rep.ok(f'all {len(counts)} (level, metric, subject) groups populate every class A/B/C')


def check_identity_invariants(rows, index: dict, rep: Report):
    """H. Identity and frontend-contract fields in schools-index.json.

    Nothing else validates these: every other check reads scores, so a school
    could lose its address and every check would still pass. The index is also
    the only eager load, so an empty field here is a visible regression in the
    search typeahead, the popup and the ?school= deep link.
    """
    rep.section('H. Identity + frontend contract (schools-index.json)')
    problems: list[str] = []

    lengths = {name: len(values) for name, values in index.items()}
    if len(set(lengths.values())) != 1:
        problems.append(f'parallel arrays differ in length: {lengths}')

    # (rspo, year) unique in the source
    seen: set[tuple] = set()
    duplicates = set()
    for row in rows:
        key = (row['rspo'], row['year'])
        if key in seen:
            duplicates.add(key)
        seen.add(key)
    if duplicates:
        problems.append(f'{len(duplicates)} duplicate (rspo, year) rows, '
                        f'e.g. {sorted(duplicates)[:3]}')

    # the JSON school set equals the source school set
    source_rspos = {row['rspo'] for row in rows}
    json_rspos = {int(rspo) for rspo in index['rspo']}
    if source_rspos != json_rspos:
        only_source = sorted(source_rspos - json_rspos)[:3]
        only_json = sorted(json_rspos - source_rspos)[:3]
        problems.append(f'school sets differ: {len(source_rspos - json_rspos)} only in xlsx '
                        f'{only_source}, {len(json_rspos - source_rspos)} only in JSON {only_json}')

    # n_years matches the source, and the contract fields are populated
    required = ('name', 'miejscowosc', 'ulica_nr', 'is_public', 'n_years', 'powiat', 'teryt')
    years_by_rspo: dict = {}
    for row in rows:
        years_by_rspo.setdefault(row['rspo'], set()).add(row['year'])
    missing_fields, wrong_years, wrong_keys = [], [], []
    for position, rspo in enumerate(int(r) for r in index['rspo']):
        for field in required:
            value = index[field][position]
            if value is None or (isinstance(value, str) and not value.strip()):
                missing_fields.append(f'rspo={rspo} {field}')
        expected_years = len(years_by_rspo.get(rspo, ()))
        if expected_years and index['n_years'][position] != expected_years:
            wrong_years.append(f'rspo={rspo} n_years={index["n_years"][position]} '
                               f'source={expected_years}')
        teryt = index['teryt'][position]
        if len(str(teryt)) != 7 or index['powiat'][position] != str(teryt)[:4]:
            wrong_keys.append(f'rspo={rspo} teryt={teryt} powiat={index["powiat"][position]}')
        if index['on_map'][position] != (index['lat'][position] is not None):
            wrong_keys.append(f'rspo={rspo} on_map disagrees with lat')
    for label, found in (('empty contract fields', missing_fields),
                         ('schools with a wrong n_years', wrong_years),
                         ('schools with an inconsistent teryt/powiat/on_map', wrong_keys)):
        if found:
            problems.append(f'{len(found)} {label}, e.g. {found[:3]}')

    rep.checked += len(json_rspos)
    if problems:
        rep.fail('identity/contract invariants violated', examples=problems)
    else:
        rep.ok(f'{len(json_rspos):,} schools: unique keys, matching sets, '
               f'equal-length arrays, contract fields populated')


def check_region_aggregates(regions, recomputed_regions, rep: Report):
    """I (spec §7.8) — every region's published aggregate must be what its own
    schools produce under §5.1's weighting, and its n_schools / n_students /
    rank / pct / n_ranked must follow from the same membership. The school-level
    checks are structurally blind to this: they never look at a region.

    The row's own identity is checked here too (spec §7.7): `parent` against the
    prefix independently derived from the geometry, and `name` / `lat` / `lon`
    for emptiness. Those three are what the choropleth and its tooltip read
    before any score, and no other check reads them at all.
    """
    rep.section('I. Region aggregates reconcile with their schools')
    for level in REGION_LEVELS:
        published = regions[level]['regions']
        expected = recomputed_regions[level]
        problems: list[str] = []
        checked = 0
        if list(published['teryt']) != expected['regions']:
            rep.fail(f'{level}: the region row set differs from the geometry '
                     f'({len(published["teryt"])} published, {len(expected["regions"])} '
                     f'recomputed)')
            continue
        lengths = {name: len(values) for name, values in published.items()
                   if isinstance(values, list)}
        if len(set(lengths.values())) != 1:
            problems.append(f'parallel arrays differ in length: {lengths}')
        if list(published['parent']) != expected['parent']:
            differing = [
                f'{teryt}: json={a} derived={b}'
                for teryt, a, b in zip(published['teryt'], published['parent'],
                                       expected['parent']) if a != b
            ]
            problems.append(f'{len(differing)} wrong parent keys, e.g. {differing[:3]}')
        checked += len(published['parent'])
        for field in ('name', 'lat', 'lon'):
            empty = [
                teryt for teryt, value in zip(published['teryt'], published[field])
                if value is None or (isinstance(value, str) and not value.strip())
            ]
            checked += len(published[field])
            if empty:
                problems.append(f'{len(empty)} regions with an empty {field}, e.g. {empty[:3]}')
        for field in ('n_schools', 'n_students'):
            wrong = [
                f'{teryt}: json={a} recomp={b}'
                for teryt, a, b in zip(published['teryt'], published[field], expected[field])
                if a != b
            ]
            checked += len(published[field])
            if wrong:
                problems.append(f'{len(wrong)} wrong {field}, e.g. {wrong[:3]}')
        for metric in EXPORT_METRICS:
            for subject in ALL_SUBJECTS:
                for field in ('score', 'rank', 'pct'):
                    published_values = published[field][metric][subject]
                    expected_values = expected[field][metric][subject]
                    checked += len(published_values)
                    tolerance = PCT_TOL if field == 'pct' else SCORE_TOL
                    wrong = [
                        f'{teryt} {metric}/{subject}.{field}: json={a} recomp={b}'
                        for teryt, a, b in zip(
                            published['teryt'], published_values, expected_values
                        )
                        if values_disagree(a, b, tolerance)
                    ]
                    if wrong:
                        problems.append(f'{len(wrong)} wrong {metric}/{subject}.{field}, '
                                        f'e.g. {wrong[:2]}')
                published_n = published['n_ranked'][metric][subject]
                checked += 1
                if published_n != expected['n_ranked'][metric][subject]:
                    problems.append(
                        f'n_ranked {metric}/{subject}: json={published_n} '
                        f'recomp={expected["n_ranked"][metric][subject]}'
                    )
        rep.checked += checked
        if problems:
            rep.fail(f'{level}: {len(problems)} aggregate mismatches', problems)
        else:
            rep.ok(f'{level}: all {checked:,} region values reconcile with their schools '
                   f'({len(expected["regions"]):,} regions)')


def check_geometry_join(regions, geometry: dict[Level, set], rep: Report):
    """J (spec §7.9) — a region without a polygon renders as nothing and a polygon
    without a region renders as a hole, and neither is visible in the data alone.
    An exact equality, because §5.1 builds the region rows from the geometry."""
    rep.section('J. Every region has a polygon and every polygon a region')
    for level in REGION_LEVELS:
        published = set(regions[level]['regions']['teryt'])
        keys = geometry[level]
        rep.checked += len(published | keys)
        if published != keys:
            rep.fail(
                f'{level}: {len(published - keys)} regions without a polygon, '
                f'{len(keys - published)} polygons without a region',
                [f'region only: {t}' for t in sorted(published - keys)[:3]]
                + [f'polygon only: {t}' for t in sorted(keys - published)[:3]],
            )
        else:
            rep.ok(f'{level}: {len(keys):,} TERYT keys match the boundary files exactly')


def _reject_json_constant(name: str):
    """json.loads calls this for the bare NaN / Infinity / -Infinity tokens, which
    are valid Python output and invalid JSON."""
    raise ValueError(f'non-JSON constant {name}')


def check_no_nan(docs_data: Path, rep: Report):
    """K (spec §7.10) — pandas yields NaN on a one-member group and json.dumps
    writes it as a bare NaN, which the browser's fetch(...).json() rejects: one
    such value takes the whole map down, far from its cause. Re-parse every
    emitted file with a parser that refuses those tokens."""
    rep.section('K. No NaN / Infinity reaches the emitted JSON')
    paths = sorted((docs_data).glob('*.json')) + shard_paths(docs_data)
    problems = []
    for path in paths:
        try:
            json.loads(path.read_text(encoding='utf-8'), parse_constant=_reject_json_constant)
        except ValueError as exc:
            problems.append(f'{path.relative_to(docs_data)}: {exc}')
    rep.checked += len(paths)
    if problems:
        rep.fail(f'{len(problems)} of {len(paths):,} files are not strict JSON', problems)
    else:
        rep.ok(f'all {len(paths):,} emitted files parse under a strict JSON parser')


def check_size_budgets(docs_data: Path, geo_dir: Path, rep: Report):
    """L (spec §7.11) — every emitted file gzipped and compared to §5.7's budget.
    A gate, not guidance: a number that cannot be met is a bug in the budget."""
    rep.section('L. Gzipped size budgets')
    breaches: list[str] = []
    checked = 0
    # group -> (worst size, its file, budget). A group is one named file, or the
    # whole shard / geometry family, whose largest member is the one at risk.
    worst: dict[str, tuple[float, str, int]] = {}

    def measure(path: Path, budget_kb: int, group: str):
        nonlocal checked
        checked += 1
        size_kb = len(gzip.compress(path.read_bytes(), GZIP_LEVEL)) / 1024
        if size_kb > worst.get(group, (0.0, '', 0))[0]:
            worst[group] = (size_kb, path.name, budget_kb)
        if size_kb > budget_kb:
            breaches.append(f'{path.name}: {size_kb:.1f} KB gzipped > {budget_kb} KB budget')

    for name, budget in FILE_BUDGETS_KB.items():
        path = docs_data / name
        if not path.exists():
            breaches.append(f'{name}: missing')
            continue
        measure(path, budget, name)
    for path in shard_paths(docs_data):
        measure(path, SHARD_BUDGET_KB, 'shards')
    for path in sorted(geo_dir.rglob('*.json')):
        measure(path, GEOMETRY_BUDGET_KB, 'geometry')

    rep.checked += checked
    if breaches:
        rep.fail(f'{len(breaches)} of {checked:,} files over budget', breaches)
    else:
        rep.ok(f'all {checked:,} files within budget')
        for group, (size_kb, name, budget_kb) in sorted(worst.items()):
            label = name if group == name else f'largest {group}, {name}'
            rep.note(f'{label}: {size_kb:.1f} KB of the {budget_kb} KB budget '
                     f'({size_kb / budget_kb:.0%})')


def check_percentile_gate(regions, recomputed_regions, rep: Report):
    """M (spec §7.5) — a region's percentile is its position among its SIBLINGS,
    so it is the siblings that must be numerous enough. The sibling count is
    derived from the docs/geo key set, never from the published parent[] array:
    reading the export's own grouping back would make the check agree with itself
    however the grouping was built. Gating on a region's own school count instead
    would wrongly publish a percentile for the gminas of 241 of 380 powiats."""
    rep.section('M. Percentile is gated on the sibling count')
    for level in REGION_LEVELS:
        published = regions[level]['regions']
        expected = recomputed_regions[level]
        siblings = expected['siblings']
        too_few_siblings = sum(
            1 for parent in expected['parent'] if siblings[parent] < MIN_PERCENTILE_N
        )
        problems: list[str] = []
        checked = 0
        suppressed: dict[str, int] = {}
        for metric in EXPORT_METRICS:
            for subject in ALL_SUBJECTS:
                scores = published['score'][metric][subject]
                percentiles = published['pct'][metric][subject]
                checked += len(percentiles)
                wrong = []
                for teryt, parent, score, percentile in zip(
                    published['teryt'], expected['parent'], scores, percentiles
                ):
                    publishable = siblings[parent] >= MIN_PERCENTILE_N and score is not None
                    if publishable != (percentile is not None):
                        wrong.append(
                            f'{teryt} {metric}/{subject}: pct={percentile}, '
                            f'siblings={siblings[parent]}, score={score}'
                        )
                if wrong:
                    problems.append(f'{len(wrong)} wrong {metric}/{subject}, e.g. {wrong[:2]}')
                if subject == 'composite_min':
                    suppressed[metric] = sum(1 for p in percentiles if p is None)
        rep.checked += checked
        if problems:
            rep.fail(f'{level}: percentile gate wrong for {len(problems)} groups', problems)
        else:
            rep.ok(f'{level}: all {checked:,} percentiles follow the sibling gate '
                   f'({too_few_siblings:,} of {len(expected["regions"]):,} regions sit under a '
                   f'parent with < {MIN_PERCENTILE_N} children)')
            rep.note('composite_min regions with no percentile: '
                     + ', '.join(f'{m}={c:,}' for m, c in suppressed.items()))


def check_reference_gate(regions, recomputed_regions, rep: Report):
    """N (spec §7.6) — a region's difference score is a distance from its PARENT's
    mean, so it is the parent's schools that must be numerous enough, and a region
    that is its parent's only child is being compared against itself. Both
    populations come from the source rows and the geometry, never from the
    published n_schools[]/parent[] arrays. mean and median carry no reference
    population at all, so nothing about them may be blanked."""
    rep.section("N. Diff scores are gated on the parent's school count")
    for level in REGION_LEVELS:
        published = regions[level]['regions']
        expected = recomputed_regions[level]
        siblings, parent_schools = expected['siblings'], expected['parent_schools']
        only_children = sum(1 for parent in expected['parent'] if siblings[parent] <= 1)
        small_parents = sum(
            1 for parent in expected['parent'] if parent_schools[parent] < MIN_REFERENCE_N
        )
        problems: list[str] = []
        checked = 0
        blanked = 0
        for metric in EXPORT_METRICS:
            weighted = metric in WEIGHTED_METRICS
            for subject in ALL_SUBJECTS:
                scores = published['score'][metric][subject]
                scored_member = expected['has_scored_member'][metric][subject]
                checked += len(scores)
                wrong = []
                for teryt, parent, score, has_member in zip(
                    published['teryt'], expected['parent'], scores, scored_member
                ):
                    gated = weighted and (
                        siblings[parent] <= 1 or parent_schools[parent] < MIN_REFERENCE_N
                    )
                    should_be_null = gated or not has_member
                    if should_be_null != (score is None):
                        wrong.append(
                            f'{teryt} {metric}/{subject}: score={score}, '
                            f'siblings={siblings[parent]}, '
                            f'parent_schools={parent_schools[parent]}, '
                            f'has_scored_school={has_member}'
                        )
                    elif gated and score is None:
                        blanked += 1
                if wrong:
                    problems.append(f'{len(wrong)} wrong {metric}/{subject}, e.g. {wrong[:2]}')
        rep.checked += checked
        if problems:
            rep.fail(f'{level}: reference gate wrong for {len(problems)} groups', problems)
        else:
            rep.ok(f'{level}: all {checked:,} scores follow the reference gate')
            rep.note(f'only children: {only_children:,}; regions whose parent holds '
                     f'< {MIN_REFERENCE_N} schools: {small_parents:,}; '
                     f'diff cells blanked by the gate: {blanked:,}')


def check_voivodeship_bijection(rows, rep: Report):
    """O (spec §7 assertion 2) — the voivodeship code takes exactly 16 values and
    the code <-> name mapping is one-to-one in both directions.

    Every grouping in the pipeline keys on the CODE (§4.1), so nothing downstream
    would raise if a source edition renamed a voivodeship or reused a name: one
    reference population would quietly split in two, or two would pool into one.
    Both directions are asserted because each catches a different fault — a
    renamed region shows up as one code with two names, a mis-keyed one as one
    name with two codes.
    """
    rep.section('O. Voivodeship codes and names are a bijection')
    width = REFERENCE_WIDTH['voivodeship']
    names_by_code: dict[Teryt, set] = defaultdict(set)
    codes_by_name: dict[str, set] = defaultdict(set)
    for row in rows:
        names_by_code[row['teryt'][:width]].add(row['wojewodztwo'])
        codes_by_name[row['wojewodztwo']].add(row['teryt'][:width])

    problems = []
    if len(names_by_code) != EXPECTED_VOIVODESHIPS:
        problems.append(f'{len(names_by_code)} distinct voivodeship codes, expected '
                        f'{EXPECTED_VOIVODESHIPS}: {sorted(names_by_code)}')
    for code, names in sorted(names_by_code.items()):
        if len(names) > 1:
            problems.append(f'code {code} carries {len(names)} names: {sorted(names)}')
    for name, codes in sorted(codes_by_name.items()):
        if len(codes) > 1:
            problems.append(f'name {name!r} carries {len(codes)} codes: {sorted(codes)}')

    rep.checked += len(names_by_code) + len(codes_by_name)
    if problems:
        rep.fail('the voivodeship code <-> name mapping is not one-to-one', problems)
    else:
        rep.ok(f'{len(names_by_code)} voivodeship codes, one name each and one code per '
               f'name ({len(codes_by_name)} names)')


def describe_key_history(entries: list[tuple[Year, str]], width: int) -> str:
    """'141204 in 2021-2025, then 141211 in 2026' — the runs of one key, in order."""
    runs: list[tuple[str, list[Year]]] = []
    for year, teryt in entries:
        key = teryt[:width]
        if runs and runs[-1][0] == key:
            runs[-1][1].append(year)
        else:
            runs.append((key, [year]))
    return ', then '.join(
        f'{key} in {years[0]}-{years[-1]}' if len(years) > 1 else f'{key} in {years[0]}'
        for key, years in runs
    )


def check_region_key_stability(rows, index: dict, rep: Report):
    """P (spec §7 assertion 4) — a school's region keys must not wander.

    Deliberately NOT a flat stability assertion at every level, because that is
    false today and a check that fails on arrival is one you learn to ignore
    rather than learn from. The three parts do different jobs:

    * voivodeship and powiat keys MUST be constant. Both are today, and a change
      there is a data fault, not a boundary reform a school can undergo.
    * The published `teryt` in schools-index.json MUST equal the school's newest
      source row. That is the pinning the pipeline actually applies (`df_recent`
      in the export), and it is what decides which gmina row a school's whole
      multi-year aggregate lands in. Nothing else compares the two.
    * A school whose gmina key genuinely changes is REPORTED, not failed, and
      named. It is a real event with a real consequence — the gmina it left
      loses a school it held for years, silently — so the count is printed every
      run whether or not it is zero.

    The full seven-digit code is noisier still: its last digit is the gmina TYPE
    (1 urban, 2 rural, 4/5 the halves of an urban-rural gmina), which moves for
    schools that never went anywhere. Keying every level on a prefix of at most
    six digits is exactly what keeps those histories in one piece, and the count
    printed below is the standing evidence for that decision.
    """
    rep.section('P. Region keys are stable across years, and the gmina pinning holds')
    history: dict[Rspo, list[tuple[Year, str]]] = defaultdict(list)
    for row in rows:
        history[row['rspo']].append((row['year'], row['teryt']))
    for entries in history.values():
        entries.sort()

    moved: dict[Level, list[str]] = {level: [] for level in REGION_LEVELS}
    type_digit_only = 0
    for rspo, entries in sorted(history.items()):
        for level in REGION_LEVELS:
            width = REFERENCE_WIDTH[level]
            if len({teryt[:width] for _, teryt in entries}) > 1:
                moved[level].append(f'rspo {rspo}: {describe_key_history(entries, width)}')
        codes = {teryt for _, teryt in entries}
        if len(codes) > 1 and len({teryt[:REFERENCE_WIDTH['gmina']] for teryt in codes}) == 1:
            type_digit_only += 1

    published_teryt = {int(rspo): str(teryt)
                       for rspo, teryt in zip(index['rspo'], index['teryt'])}
    mispinned = [
        f'rspo {rspo}: index={published_teryt.get(rspo)} newest source row={entries[-1][1]}'
        for rspo, entries in sorted(history.items())
        if published_teryt.get(rspo) != entries[-1][1]
    ]

    rep.checked += len(history) * len(REGION_LEVELS) + len(history)
    problems = []
    for level in ('voivodeship', 'powiat'):
        if moved[level]:
            problems.append(f'{len(moved[level])} schools change their {level} key: '
                            f'{moved[level][:3]}')
    if mispinned:
        problems.append(f'{len(mispinned)} schools pinned to a gmina that is not their '
                        f'newest source row: {mispinned[:3]}')
    if problems:
        rep.fail('region keys are not stable', problems)
    else:
        rep.ok(f'{len(history):,} schools: voivodeship and powiat keys constant across '
               f'years, every published teryt its newest source row')

    rep.note(f'schools whose gmina key changes across years: {len(moved["gmina"])}')
    for line in moved['gmina'][:10]:
        rep.note(f'  {line}')
    if len(moved['gmina']) > 10:
        rep.note(f'  … and {len(moved["gmina"]) - 10:,} more')
    rep.note(f'schools whose 7-digit teryt changes but whose gmina does not: '
             f'{type_digit_only:,} — the gmina TYPE digit, which is why every level '
             f'keys on a prefix of at most six')


# ── main ─────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    """Command-line arguments: where the source xlsx, the served JSON and the
    boundary polygons live."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        '--data-dir',
        default='data/egzamin-osmoklasisty',
        type=Path,
        help='directory with the source xlsx files',
    )
    parser.add_argument(
        '--docs-data',
        default='docs/data',
        type=Path,
        help='directory with schools-index.json, scale.json, regions-*.json and powiat/',
    )
    parser.add_argument(
        '--geo-dir',
        default='docs/geo',
        type=Path,
        help='directory with the PRG boundary GeoJSON (kraj.json, woj/, pow/)',
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
    scale, index_payload, region_payloads = load_json_exports(args.docs_data)
    index = index_payload['schools']
    json_years = set(scale['metadata']['years_in_data'])
    print(
        f'  schools-index.json: {len(index["rspo"]):,} schools; years {sorted(json_years)}; '
        f'metrics {scale["metadata"]["metrics"]}; '
        f'reference levels {list(scale["school"])}'
    )
    for level in REGION_LEVELS:
        print(f'  regions-{level}.json: '
              f'{len(region_payloads[level]["regions"]["teryt"]):,} regions')

    print('Reading boundary geometry …')
    geometry = {level: geometry_keys(args.geo_dir, level) for level in REGION_LEVELS}
    print('  ' + ', '.join(f'{level}: {len(keys):,}' for level, keys in geometry.items()))

    print('Reading source xlsx …')
    rows = filter_rows_to_json_years(read_clean_rows(args.data_dir), json_years)

    print('Recomputing metrics / views / ranks / regions independently …')
    view_key_to_rspo_scores = compute_view_scores(rows)
    students_by_rspo = compute_students_per_school(rows)
    teryt_by_rspo = latest_teryt_per_school(rows)
    recomputed_regions = {
        level: compute_region_level(
            level, geometry[level], teryt_by_rspo, students_by_rspo, view_key_to_rspo_scores
        )
        for level in REGION_LEVELS
    }
    print(f'  {len(view_key_to_rspo_scores):,} view populations, '
          f'{sum(len(v) for v in view_key_to_rspo_scores.values()):,} school scores')

    rep = Report()
    check_subjects_vs_xlsx(rows, args.docs_data, rep)
    check_aggregates(view_key_to_rspo_scores, args.docs_data, rep)
    check_completeness(view_key_to_rspo_scores, args.docs_data, rep)
    check_index_shard_join(index, args.docs_data, rep)
    check_ranks(view_key_to_rspo_scores, args.docs_data, rep)
    check_metadata(scale, region_payloads, view_key_to_rspo_scores, recomputed_regions, rep)
    check_class_spread(scale, args.docs_data, rep)
    check_identity_invariants(rows, index, rep)
    check_region_aggregates(region_payloads, recomputed_regions, rep)
    check_geometry_join(region_payloads, geometry, rep)
    check_no_nan(args.docs_data, rep)
    check_size_budgets(args.docs_data, args.geo_dir, rep)
    check_percentile_gate(region_payloads, recomputed_regions, rep)
    check_reference_gate(region_payloads, recomputed_regions, rep)
    check_voivodeship_bijection(rows, rep)
    check_region_key_stability(rows, index, rep)

    print(f'\n{"=" * 64}')
    if rep.failures == 0:
        print(f'ALL CHECKS PASSED  ({rep.checked:,} comparisons)')
        return 0
    print(f'{rep.failures} CHECK(S) FAILED  ({rep.checked:,} comparisons)')
    return 1


if __name__ == '__main__':
    sys.exit(main())
