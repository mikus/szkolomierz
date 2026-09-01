# CLAUDE.md — Technical reference for the school quality project

This file is the technical reference for an AI agent (Claude Code) working on this
project. It documents the data, conventions, methodology decisions, and output
formats. For a human-facing overview, see `README.md`.

---

## What this project does

Analyses results of the Polish 8th-grade exam (**egzamin ósmoklasisty**) published
by **CIE** at `mapa.wyniki.edu.pl` and produces data for an external
school-quality map.

**Critical scope fact:** the data covers **all of Poland** — 16 voivodeships,
380 powiats, 2,479 gminas and 12,889 schools over 2021–2026. The system used to
cover a single voivodeship, from OKE Warszawa files — which is where the
repository's name comes from, and renaming it is deliberately out of scope. That
data scope is gone, and so is the standing rule that went with it ("always use
*voivodeship* rather than *national*"), which this section replaces.

### Reference levels — the vocabulary that replaced that rule

A school's difference-based score is its distance from the per-year mean of some
population. **Which population is a parameter, not a constant**, and it has a
name: the **reference level**, one of `national`, `voivodeship`, `powiat`,
`gmina` (`REFERENCE_LEVELS` in `src/school_quality/levels.py`). All four are
computed and exported; the map's "reference point" control picks between them,
defaulting to `voivodeship` (`DEFAULTS.baseline` in `docs/app.js`).

So do **not** write `voivodeship_mean` or `national_mean` as though one of them
were the truth. Write `reference` / `reference_level`, and name the level
wherever a number could be mistaken for one at another level. `mean` and
`median` are the exception that proves the rule: they are raw 0–100 aggregates
with **no** reference population, so they are computed once, at
`PRIMARY_REFERENCE_LEVEL`, and do not vary by level at all.

Two further rules follow, and both are load-bearing:

- **A region is scored against the level above it** (`src/school_quality/zoom.py`):
  a voivodeship nationally, a powiat within its voivodeship, a gmina within its
  powiat. Scoring a level against itself puts every region at ~0 by construction
  and flattens the view at exactly the zoom where contrast is the point.
- **Small populations are withheld, not shown** (`suppression.py`,
  `aggregate.py`): a score needs a reference population of at least
  `MIN_REFERENCE_N = 5` schools *and* a parent with more than one child; a
  percentile needs `MIN_PERCENTILE_N = 8` siblings. The two gate different
  populations and must not be conflated — see `aggregate.py`'s module docstring.

---

## Repository layout

```
compare-primary-schools-mazowieckie/
├── notebooks/
│   ├── how_to_measure_school_quality.ipynb   # the analysis + export (run end to end)
│   └── …-2021-2025.ipynb, …-old-approach-…   # superseded; kept as historical records
├── src/school_quality/                       # pure functions the notebook imports
│   ├── address.py  aggregate.py  geometry.py # (src/ is on the path, not installed)
│   ├── levels.py   rspo.py       sources.py
│   └── suppression.py  teryt.py  zoom.py
├── tests/                                    # pytest over src/ and the script helpers — fast, no I/O
├── scripts/
│   ├── fetch_sources.py                      # download the pinned CIE source files
│   ├── fetch_geometry.py                     # download the PRG polygons → docs/geo/
│   ├── geocode_schools.py                    # RSPO, then Nominatim; both gated on the voivodeship
│   └── validate_export.py                    # check the published JSON against the source xlsx
├── data/                                     # INPUT (read-only source data)
│   ├── egzamin-osmoklasisty/                 # CIE xlsx files, national, one per year
│   │   ├── 2021 - E8_2021_szkoly_09.xlsx
│   │   ├── 2022 - E8_2022_szkoly_09.xlsx
│   │   ├── ...
│   │   └── SOURCES.csv                       # provenance of each xlsx (see below)
│   ├── school_coords.csv                     # coordinate cache (rspo, address, lat, lon)
│   └── school_coords_unmapped.csv            # manual-triage list, rewritten every run
├── output/                                   # OUTPUT for analysts (xlsx)
│   └── schools-{metric}.xlsx   × 4
├── docs/                                     # the map app (GitHub Pages serves this)
│   ├── index.html, ranking.html, help.html   # the frontend (see MAP_APP_BRIEF.md)
│   ├── app.js, map.js, ranking.js, help.js, style.css
│   ├── data/                                 # JSON consumed by the app (notebook writes here)
│   │   ├── schools-index.json
│   │   ├── scale.json
│   │   ├── regions-{level}.json   × 3
│   │   └── powiat/{teryt4}-{metric}.json   × 4 × 380
│   └── geo/                                  # PRG boundary polygons (fetch_geometry.py)
│       ├── kraj.json
│       ├── woj/{ww}.json     × 16
│       └── pow/{wwpp}.json   × 380
├── README.md
├── CLAUDE.md
└── MAP_APP_BRIEF.md                          # build spec for the map app (frontend)
```

The notebook writes the **JSON** files (for the map) into `docs/data/` and the
**xlsx** files (for analysts) into `output/`. This avoids a copy step: the data
the app serves is generated straight into the directory GitHub Pages publishes.
`school_coords.csv` (coordinate cache) stays in `data/`. `docs/geo/` is written
by `fetch_geometry.py`, not by the notebook — boundaries change a few times a
decade, so they are fetched deliberately and committed.

`data/` and `output/` are both singular mass nouns (input data / output data),
paralleling each other. `notebooks/` and `scripts/` are plural (countable files).

---

## Source data format

Each CIE xlsx has a sheet named `SAS` with a two-level header. After loading and
normalising (lowercase, strip Polish diacritics, collapse whitespace), the
relevant columns are:

**Metadata columns** (level-0 group is blank / "meta"):
- `rspo` — unique school identifier (stable across years)
- `nazwa szkoly` — school name
- `czy publiczna` — public/private flag
- `powiat - nazwa`, `gmina - nazwa`, `typ gminy` — administrative geography
- `miejscowosc`, `ulica nr` — address (the geocoder's fallback route)
- `wojewodztwo - nazwa` — **sixteen** values; the loader asserts `nunique() == 16`
  rather than a single expected name
- `kod teryt gminy` — the 7-character TERYT key, normalised by
  `school_quality.teryt.normalise_teryt`. This is what joins schools to regions
  and to the boundary polygons; slice `[:2]` / `[:4]` / `[:6]` for voivodeship /
  powiat / gmina. Keep it a `str` — a leading zero lost to an int cast silently
  moves a school to another voivodeship
- `id oke`, `rodzaj placowki` — carried through the loader

**Per-subject columns** (level-0 group is the subject name):
- `liczba zdajacych` — number of students who sat the exam
- `wynik sredni (%)` — mean score
- `mediana (%)` — median score
- (also `odchylenie standardowe (%)`, `modalna (%)` — not currently used)

Subjects present: `polski`, `matematyka`, `angielski`, and several minor foreign
languages (`francuski`, `hiszpanski`, `niemiecki`, `rosyjski`, `wloski`).

### The address column is not equally good every year

`ulica nr` degraded in the 2026 file: it drops the `ul.` marker and the leading
words of the street name — `ul. 3 Maja 27` → `Maja 27`, `ul. Adama Mickiewicza
126/128` → `Mickiewicza 126/128`. Measured nationally, of **12,116** schools
present in both 2025 and 2026: 6,895 unchanged, 3,933 lost only the street-type
prefix, 1,228 lost leading words with the house number still matching, 1 lost a
*number* as well (which makes the address wrong), and 59 changed for real.

So **never assume the newest year has the best address.** The notebook picks one
address per school by walking its years oldest → newest and keeping a current
value; a newer address replaces it only if it is *not* a shortened form of it
(see "Address selection" under the notebook structure). Declined updates are
written to `output/rejected_addresses.csv`.

### Provenance — `data/egzamin-osmoklasisty/SOURCES.csv`

Every source xlsx must be documented in `SOURCES.csv` (columns: `file_name,
year, edition, oke, wojewodztwo, webpage, document_link, retrieved_date,
notes`). `file_name` is the exact on-disk name (including any `.xlsx.xlsx`
double extension) and is the join key.

The notebook's load cell **raises** if any source file present in
`data/egzamin-osmoklasisty/` is missing from `SOURCES.csv` (and warns if
`SOURCES.csv` lists a file not on disk). This forces recording where each file
came from whenever a new one is dropped in. Dotfiles (e.g. LibreOffice
`.~lock.*`) are ignored — they are not source data.

Note: the loader only ingests files whose name **starts with the year**
(`YEAR_FILE_RE = ^\d{4}`).

---

## Working DataFrame conventions

The notebook builds a flat `df` with one row per (school, year) and these columns:

- `rspo`, `year`, `school_name`, `is_public`
- `wojewodztwo`, `gmina`, `powiat`, `typ_gminy`, `miejscowosc`, `ulica_nr`
- `teryt` (7-char `str`), `id_oke`, `rodzaj_placowki`
- Per subject `s`: `n_{s}`, `mean_{s}`, `median_{s}`
  (e.g. `n_polski`, `mean_matematyka`, `median_angielski`)

`add_level_keys` (`levels.py`) derives `teryt_wojewodztwo` / `teryt_powiat` /
`teryt_gmina` from `teryt`; those are the group keys every reference level is
computed over.

Only **3 core subjects** are usable for quality scoring: `polski`, `matematyka`,
`angielski`. The minor languages are taken by too few students per school to be
statistically meaningful (`core_short = ['polski', 'matematyka', 'angielski']`).

`ALL_YEARS` is the sorted list of years present in the data. `rspo_all_years` is
the set of schools with data in every year (used for stability analysis that needs
a constant fold population).

---

## The metric (final decision)

### Per-year, per-subject normalised score

```
diff_mean_year(school, subject, year, level) =
    school_mean(subject, year) − reference_mean(subject, year, level)

unit_norm_diff_mean_year =
    diff_mean_year / (100 − reference_mean)   if diff_mean_year ≥ 0
    diff_mean_year / reference_mean           if diff_mean_year < 0
```

Range [−1, +1]: 0 = at the reference mean, +1 = at the ceiling (100%),
−1 = at the floor (0%). In practice values rarely exceed ±0.5.

`reference_mean(subject, year, level)` = mean of all schools' `mean_{subject}` in
that year, within the school's region **at that reference level** — computed by
`levels.attach_reference`, which broadcasts with a merge rather than
`Series.map` (against a MultiIndex-keyed Series `.map()` returns all-NaN without
raising, silently zeroing every downstream metric). Grouping by year is what
neutralises exam-difficulty drift — the Maths reference jumped ~14 pp between
2021 and 2022.

**All four levels are computed for every school**, and the export carries all
four. `PRIMARY_REFERENCE_LEVEL = 'voivodeship'` is the level the analysis half of
the notebook and the xlsx exports use; it is also what `mean` and `median` are
computed at, since they have no reference population and so exist only once.

### Aggregation across years

The per-school score aggregates the yearly values. **The aggregation method
depends on whether the metric is a baseline or an advanced one:**

| Metric | Aggregation across years | Why |
|--------|--------------------------|-----|
| `mean` (baseline) | arithmetic mean (equal weight) | simplest, for users who want a plain baseline |
| `median` (baseline) | arithmetic mean (equal weight) | same |
| `diff_mean` (advanced) | weighted mean by `n_students` | more evidence from larger cohorts |
| `unit_norm_diff_mean` (advanced, **primary**) | weighted mean by `n_students` | same |

This is encoded in `AGGREGATION_BY_METRIC` in the export section.

### Why `unit_norm_diff_mean` with weighted mean

Chosen by **leave-one-out (LOO) jackknife stability** testing: for each school
with ≥ 2 years, compute the score with each year left out; the metric whose LOO
estimates are closest together (lowest LOO standard deviation, normalised by the
metric's overall spread) is the most stable. Tested 8 per-year metrics × 5
aggregation methods, on the 2022-onward population as well (9,095 schools,
including the small ones that only started reporting after 2021).

**Read the size-bin tables before repeating a summary of them.** They are
rendered by `render_min_highlighted_table(..., axis=1)`, so the highlighted cell
is the per-row minimum — one winner per (subject, aggregation) row, 25 rows per
subject. On the national data `diff_mean` and `unit_norm_diff_mean` still win
throughout the **1–9, 10–19, 20–49 and 50–99** bins. In the **100+ bin they no
longer do**: the percentile-based `pct_mean` takes 9 of that bin's 15 rows — all
five in Maths, four in English — and `diff_mean` survives only in Polish.

The primary metric is unchanged, and deliberately so: `pct_mean` is a within-year
percentile rank, which discards *how far* a school sits from its reference — the
very quantity the colour scale, the ±0.33σ band and the region aggregates are
built on. Revisiting that trade would be a data decision needing its own spec
section, not a silent swap. What must **not** survive is the older claim that the
difference metrics win at every school size; the tables contradict it.

`diff_mean` and `unit_norm_diff_mean` correlate at Spearman 1.000 — identical
rankings, different scales. `diff_median` (median-based) is consistently *worse*
than `diff_mean`, because the median responds more violently to year-to-year
difficulty shifts (the median student moves the full shift, while the mean is
damped by floor/ceiling effects).

---

## Composite across subjects

```
composite_min(school) = min(
    unit_norm_diff_mean_polski,
    unit_norm_diff_mean_matematyka,
    unit_norm_diff_mean_angielski,
)
```

The **minimum** (not mean) of the three subject scores — answers "is this school
weak in *any* subject?". This is the **primary value shown on the map**.

`good_in_all_3` has been **removed** — do not reintroduce it.

The three subjects correlate ~0.7–0.8 (Pearson), so they are informative but not
redundant; the min captures the bottleneck subject.

---

## Colour scale (for the map)

**3 classes** by distance from the centre, boundary ±0.33σ, computed **per
(reference level, metric, subject)** for schools and **per (region level, metric,
subject)** for regions:

| Class | Condition | Flat colour |
|-------|-----------|-------------|
| **A** above average | score > centre + 0.33σ | green `#1a9850` |
| **B** around average | centre − 0.33σ ≤ score ≤ centre + 0.33σ | yellow `#fde08a` |
| **C** below average | score < centre − 0.33σ | red `#d6604d` |

Legend labels are deliberately neutral — "powyżej / w okolicy / poniżej średniej"
(above / around / below average), **not** "good / weak": a school just past the
±0.33σ boundary doesn't deserve a value judgement the data can't support (§7).
"Średnia" alone is avoided for B because it clashes with the `mean` metric's
display label ("Średnia").

The ±0.33σ band is wider than the multi-year base score's own year-to-year noise
(≈ 0.12σ, from LOO), so the three buckets are statistically distinguishable. The
old 5-class scheme (extra ±1.5σ "saturated" cutoffs) was dropped — ±1.5σ was
arbitrary and median-angielski left class A empty (centre + 1.5σ > 100).

A **gradient toggle** (map "Ustawienia", default **on**; ranking class column
always gradient) renders a continuous colour instead of 3 flat ones: B stays flat
yellow (muddy middle, §7), A ramps yellow→green and C ramps yellow→red out to the
**1st / 99th percentile** of the actual score distribution (robust to outliers,
so one extreme school can't stretch the scale).

**p1/p99 are exported, not computed in the browser.** They used to be derived
client-side by sorting every loaded school's score, which was fine when one file
held every school. With per-powiat shards that would sort a median of 27 schools
and colour the same school differently depending on which shard happened to load
first. They now come from `scale.json` alongside `sigma`/`sigma_centre`
(`scaleFor` in `docs/app.js`), computed nationally per reference level.

σ and centre are computed **per metric and per subject**, because the metrics
live on different scales (`mean`/`median` are 0–100; `diff_mean` and
`unit_norm_diff_mean` are difference scales). The rules:

- **`mean` and `median`** (raw 0–100 scale): centre = the mean of school scores
  for that subject (≈ 50–64 by subject), σ = std across schools. Centring at 0
  would make no sense — no school scores 0%. These do not vary by reference
  level; the same numbers are stored under all four keys.
- **`diff_mean` and `unit_norm_diff_mean`** (difference scales): centre = 0 for
  the three subjects (already centred by construction), σ = std across schools.
- **`composite_min`** (any metric): centre = the empirical *mean* of composite_min
  for that metric. composite_min's distribution is shifted left (the minimum of 3
  draws is systematically below each draw), so centring on its own mean gives a
  usable map instead of one where almost everything is red.

All of these live in **`docs/data/scale.json`** under
`school[level][metric][subject] = {sigma, sigma_centre, p1, p99}`, so the
frontend can colour the map for any metric, subject *and* reference level. The
region files carry their own `metadata.sigma` / `metadata.sigma_centre`, computed
over that level's region scores — region aggregates are far less spread out than
individual schools, so colouring them on the school scale would leave the
choropleth almost uniformly yellow.

Indicative school-level `unit_norm_diff_mean` σ at the **national** reference
level (recomputed each run): polski ≈ 0.172, matematyka ≈ 0.240,
angielski ≈ 0.279, composite_min ≈ 0.207. They shrink as the reference narrows —
at the powiat level, ≈ 0.147 / 0.205 / 0.233 / 0.175.

All four metrics × four subjects are exported, so the user can toggle both the
metric and the subject that colours the map.

### Primary metric vs the app's default — deliberately different

`unit_norm_diff_mean` is the **primary metric**: the one the LOO stability test
picked, and the one `metadata.default_metric` names in `scale.json`.

The **app opens on `mean`** (subject `composite_min`). That is a UI decision, not
a statistical one. `unit_norm_diff_mean` renders as an unlabelled decimal near
zero; a reader looking for "56%" concludes the page is broken rather than that it
is precise. `mean` answers the question people arrive with, and the normalised
score is one entry away in the same dropdown.

So the two can disagree, and the frontend **never reads
`metadata.default_metric`** — the UI default lives in `DEFAULTS.metric`
(`docs/app.js`) alone. Don't "fix" the metadata to match the UI: the field
records which metric the analysis endorses. Changing the app's default is a
one-line edit in `app.js` and needs no re-export.

`median` and `diff_mean` sit behind the app's "advanced metrics" toggle
(`BASIC_METRICS` in `docs/app.js`): `diff_mean` ranks identically to
`unit_norm_diff_mean` (Spearman 1.000), and `median` lost the LOO test to `mean`.
Both stay fully present in the JSON and xlsx exports.

---

## Notebook structure (`how_to_measure_school_quality.ipynb`)

- **0. Setup** — imports, `DATA_DIR`, `OUTPUT_DIR`, helper `render_min_highlighted_table`
- **1. Load data** — read all xlsx, build flat `df`, drop rows missing core subjects
- **2. Why only 3 subjects** — student-count distributions justify dropping minor languages
- **3. Choosing the best per-year metric** — the LOO stability analysis:
  - why the median jumps more than the mean (difficulty shifts)
  - within-school year-to-year swing vs the reference level's own swing
  - candidate metrics + aggregation methods (joint LOO test, 8 × 5)
  - rank-swing analysis + the density-effect explanation
- **4. Final metric definition** — formulas, why, colour scale; subsection
  "Combining all three subjects" (correlation, composite_min, colour-class counts)
- **5. How school level and rank changes** — base vs LOO vs single-year views;
  lollipop charts for two samples (12 schools = 4 top/4 mid/4 bottom; 15 schools
  = 3 each at P10/30/50/70/90); population-wide scatter of range and min/max
- **6. Export data to external map** — computes alternative views at all four
  reference levels, then writes `schools-index.json`, `scale.json`,
  `regions-{level}.json` × 3, the per-powiat shards, and the analyst xlsx

### Address selection (`select_address`, Section 6)

One address per school, chosen by walking its years oldest → newest and keeping a
current value. A newer address replaces it **unless it is a shortened form**:
same town, same house number, and its street words appearing as a contiguous run
inside the current street words. Anything else — different street, different
house number, different town — is a real change and wins.

Comparison details that matter:

- **Town and street are compared separately.** Concatenating them breaks the
  contiguity test whenever `ul.` sits at the boundary: `Raczyny | ul. Kopernika 5`
  tokenises to `(raczyny, ul, kopernika, 5)` and `Raczyny | Kopernika 5` to
  `(raczyny, kopernika, 5)` — the shorter is not a contiguous run of the longer.
- **Tokens split on punctuation, not just spaces**, so `Grota-Roweckiego` matches
  `Grota Roweckiego`, and `Ks.Kan.E.Sierbińskiego` matches `ks. kan. E. Sierbińskiego`.
- **The last token (house number) must match.** Without it `Krynoliny 9` would
  count as a shortened form of `Krynoliny 9/11`, and `Szkolna 1` of `Szkolna 12`.
- **`al. Aleja …` / `pl. Plac …` / `os. Osiedle …` collapse to the full word.**
  The abbreviation carries nothing the next word doesn't; the publisher cleaned
  this up between 2024 and 2026, so collapsing keeps one spelling across years. A lone
  `ul.` is kept — it is the only street-type marker present.

On the 2021–2026 national data this declines **5,135 updates across 5,129
schools** (the notebook prints both, and `output/rejected_addresses.csv` lists
them). Since Task 3 the primary coordinate route is the RSPO register keyed by
school id, which the address column cannot degrade at all; address quality now
matters only for the Nominatim fallback.

### Helper: `render_min_highlighted_table(df, caption, value_fmt='{:.3f}', axis=1)`

Renders a DataFrame as an HTML table with the **minimum cell highlighted green**,
using **inline `<td style="...">`** (not a `<style>` block). This is required
because VS Code and nbconvert strip `<style>` blocks from notebook outputs, so
pandas `Styler.highlight_min` / `.apply` colouring does not survive. `axis=1`
highlights the min per row; `axis=0` per column.

---

## Export (Section 6)

### Views

For each (school, subject, metric), four **views** are exported — each computed
only over the **years the school actually has** (no meaningless folds):

| view_kind | view_param | meaning |
|-----------|-----------|---------|
| `base` | — | score over all the school's years |
| `loo` | excluded year | score with one year left out (only if ≥ 2 years) |
| `single_year` | year | score from one year alone |
| `last_k` | k | score over the most recent k years, k = 2 … (n_years − 1) |

Each view carries `score`, `rank` (1 = best, among schools present in that view),
`pct` (percentile), and `n_students` (the **median** number of students per year
in that view — the school's typical cohort size, rounded. Median rather than sum
or mean: summing across overlapping views is meaningless and would drift up as
years accumulate; the median is robust to anomalous years — e.g. a
home-schooling-linked school that grew from 5 to 1200 students should report its
typical size, not a mean dragged by the extremes (~16% of schools have mean and
median diverging by >5 students). For `composite_min`, the cohort of the subject
that produced the minimum — so a validator knows which subject and how many
students the composite value came from).

### Output files

- **`docs/data/schools-index.json`** (~2.3 MB raw) — identity only, and the one
  file every page loads. **Parallel arrays**, not an array of objects: `rspo`,
  `name`, `teryt`, `powiat`, `miejscowosc`, `ulica_nr`, `is_public`, `n_years`,
  `lat`, `lon`, `on_map` — all 12,889 entries long, index `i` being one school.
  `lat`/`lon` come from the coordinate cache (`null` if missing; 3 schools today).
  It carries **no scores**: those live in the shards, so switching metric or
  reference level does not re-download identity.
- **`docs/data/scale.json`** (~10 KB) — everything needed to turn a score into a
  colour. `school[level][metric][subject] = {sigma, sigma_centre, p1, p99}` for
  the four reference levels, plus `metadata.slider_ranges[level][metric] =
  {min, max, p1, p99, step}` and `metadata.default_metric` / `metrics` /
  `subjects` / `years_in_data`.
- **`docs/data/regions-{level}.json`** × 3 (voivodeship ~13 KB, powiat ~230 KB,
  gmina ~1.5 MB) — what the choropleth draws. Parallel arrays again: `teryt`,
  `name`, `parent`, `lat`, `lon`, `n_schools`, `n_students`, plus
  `score[metric][subject]`, `rank[metric][subject]`, `pct[metric][subject]` and
  the rank denominator `n_ranked[metric][subject]`. **Row identity comes from the
  polygons, not from the schools** — a gmina the register knows but no scored
  school sits in is a real row with `n_schools = 0` and a neutral fill, not a
  hole in the map. `score` is `null` where the suppression gate withheld it;
  `rank` is national, `pct` is among siblings. `metadata` carries this level's own
  `sigma` / `sigma_centre` and the two thresholds (`min_reference_n`,
  `min_percentile_n`).
- **`docs/data/powiat/{teryt4}-{metric}.json`** (4 metrics × 380 powiats = 1,520
  files, ~1.04 GB total; median 0.53 MB, p90 1.2 MB, largest 8.2 MB for Warszawa)
  — every *view* for the schools of one powiat, keyed
  `schools[rspo][level][subject][view]`. `base` is a flat `{score, rank, pct}`;
  `loo` / `single_year` / `last_k` are `{param: {score, rank, pct}}` with
  integer-string keys (`"2021"`, `"2"`). Ranks and percentiles inside are
  **national**, not powiat-scoped — the shard is a delivery unit, not a
  population. Fetched one powiat at a time, which is the whole reason for the
  split: a single national file at four reference levels is the gigabyte above.
- **`docs/geo/`** (~48 MB) — the PRG boundary polygons keyed by
  `properties.JPT_KOD_JE` (= TERYT). Written by `scripts/fetch_geometry.py`, not
  by the notebook, and committed.
- **`output/schools-{metric}.xlsx`** × 4 (58.0 / 61.2 / 63.2 / 63.4 MB; `output/`
  totals 247 MB) — long format for analysts, **754,388 data rows each**, one row
  per (school, subject, view), in two sheets:
  - **`data`** sheet columns: `rspo, school_name, miejscowosc, ulica_nr, powiat,
    gmina, typ_gminy, is_public, n_years, metric, subject, view_kind, view_param,
    score, rank_overall, pct_overall, n_in_view, n_students`.
  - **`legend`** sheet: a human-readable description of the metric, the
    across-years aggregation method, and every column / view_kind / subject — so
    someone validating a school's number knows exactly how it was computed.

  These files grew roughly tenfold with the national swap — the long frame went
  from tens of thousands of rows to 754,388 — which is why the export **stays at
  `PRIMARY_REFERENCE_LEVEL` only**. Emitting all four reference levels would take
  one metric's frame to ~2.9M rows, nearly three times Excel's ceiling. At
  754,388 the sheet already sits at 72% of Excel's 1,048,575-row limit, and the
  frame grows with the year axis, so `build_long_frame_for_metric` asserts against
  that ceiling (spec §5.8): it fails loudly rather than truncating silently. When
  it eventually fires, split the export per voivodeship — do not drop views.
- **`output/rejected_addresses.csv`** — every address update the pipeline declined
  as a shortened form: `rspo, school_name, year, kept_miejscowosc, kept_ulica_nr,
  declined_miejscowosc, declined_ulica_nr, dropped_words`. The rejection reason is
  the same for every row, so instead of a constant comment column the report names
  the words that would have been lost (`dropped_words`). Same contract as
  `output/rejected_schools.csv`: whatever the pipeline drops is written out.

### Slider ranges (value filter config)

`scale.json` → `metadata.slider_ranges[level][metric] = {min, max, p1, p99, step}`
gives the frontend the range for the map's "show schools with score above X"
filter, per **reference level** and metric (the scale differs both ways: `mean`
is 0–100 at every level, `unit_norm_diff_mean` is ≈ −0.90…+0.81 nationally and
≈ −0.90…+0.78 at gmina level). `p1`/`p99` are robust default slider ends;
`min`/`max` are hard limits. The config is computed at export time — data and
config generated together, so they can't drift — rather than recomputed in the
browser over whatever shard happens to be loaded.

Naming: **English** for technical fields, **Polish** for geographic fields
(miejscowosc, ulica_nr, powiat, gmina, typ_gminy).

### Idempotence

`FORCE_REGENERATE = False` (top of Section 6). On each run the export compares
new data with the existing file and **skips writing if unchanged**, so git stays
clean on no-op runs:
- JSON: compares parsed payloads, ignoring `metadata.generated_at`.
- XLSX: reads the existing file (`dtype={'view_param': str}` to avoid `'2'`→`2.0`
  drift) and compares with `dataframes_equal` (floats via `np.isclose`,
  `rtol=1e-6`). `view_param` is written as text format so Excel doesn't coerce it.

`FORCE_REGENERATE = True` rewrites everything. `created` timestamps reflect the
real generation time when a file is actually written.

---

## Geocoding (`scripts/geocode_schools.py`)

Coordinates are **not** in the exam data, so they are resolved separately.

**RSPO first, Nominatim only as a fallback.** RSPO is the Polish school register;
its institution record carries a geotag, so a school's coordinates can be looked
up by its **school id** rather than by its address text. That matters beyond
speed: the address column degraded in 2026 (above), and an id-keyed lookup is
immune to it. Only a school with no usable RSPO geotag falls through to address
geocoding. The pure half — URL construction and payload parsing — is
`src/school_quality/rspo.py`, so it is testable without a network.

- **Input**: `docs/data/schools-index.json` (rspo + `teryt` + address).
- **Cache**: `data/school_coords.csv` with columns
  `rspo, miejscowosc, ulica_nr, latitude, longitude`.
- **Logic**: if an rspo is in the cache and its address is unchanged, keep the
  cached row **in its original CSV position**; if the address changed, re-resolve
  in place; new schools are **appended at the end**.
- **Contact**: `NOMINATIM_CONTACT` (or `--contact`) is needed **only for the
  fallback**. Without it the script warns, skips Nominatim, and still runs every
  RSPO lookup.
- **Flags**: `--limit N` (cap new requests, for testing), `--force` (ignore
  cache), `--report-only` (regenerate the reports, geocode nothing).

### The voivodeship gate (both routes)

Every coordinate this script writes — register geotag and geocoded address
alike — must fall inside the polygon of the voivodeship the **exam data**
assigns the school, taken from the first two digits of its `teryt` and tested
against `docs/geo/kraj.json`. The point-in-polygon itself is
`src/school_quality/geometry.py`; the boundaries are already committed for the
map, so the gate costs no new data and no network call.

A bounding box is not enough and was the original defect: Poland's voivodeships
interlock, so a box around one covers large parts of four others, and a
same-named village on the far side of the country passed for a match.

**A rejected coordinate is dropped, never relocated.** The school goes to the
unmapped triage report instead. A marker 600 km from the school it names is not
partial information — it is wrong information wearing the same confidence as
everything else on the map, and a reader has no way to tell. "We do not know
where this is" is the true statement.

A rejected **register** geotag is additionally listed by name at the end of the
run (`print_rejected_geotags`). It is not a geocoding failure: it means RSPO and
the OKE file place the school in different voivodeships, and one of those two
authoritative sources is wrong. That is a data-quality question worth chasing,
not something to bury in an unmapped count.

`scripts/validate_export.py` check Q is the standing gate on the published side:
every `lat`/`lon` in `schools-index.json` must fall inside its own **powiat**
polygon. It carries a `KNOWN_MISPLACED_RSPO` grandfather list, which is expected
to shrink and never to grow — see the comment there.

### Nominatim fallback: 3 attempts, no centroid fallback

Used only where RSPO has no usable geotag, or where its geotag failed the gate.
All three attempts are `bounded=1` to the school's own voivodeship's bounding
box, and the geocoder accepts the first result that falls inside that
voivodeship's **polygon**:

1. **Structured query** — `street=<clean street>`, `city=<miejscowosc>`,
   `country=Polska`, `countrycodes=pl`.
2. **Free-text** — `q="<clean street>, <miejscowosc>, Polska"`.
3. **Free-text with original prefixed street** — same as (2) but keeping the
   original `ul. X` form (some streets disambiguate better with the prefix).

**No voivodeship name goes into the query.** The region is expressed as a
viewbox, never as words: asserting one in free text degrades Nominatim's text
ranking rather than helping it. This rule survives the gate above — the gate
constrains and filters, it does not rewrite the query text.

"Clean street" = the original `ulica_nr` with leading `ul./Ul./al./Al./pl./Pl./os./Os.`
stripped. Results outside the voivodeship are rejected even when returned — the
viewbox is a soft bias, not a hard filter.

**The cache key strips the same prefix.** `normalize_address` (used to decide
whether a cached row is still valid) applies `_strip_street_prefix`, so
`ul. Kopernika 5` and `Kopernika 5` are one address. They produce an identical
query, so treating them as different would re-geocode a school for a result that
cannot change — which is exactly what the 2026 file would have triggered for ~570
schools. Keep these two normalisations in step: whatever the query ignores, the
cache key must ignore too.

**No town-only fallback.** If RSPO has nothing and all three Nominatim
strategies fail, the geocoder writes an empty `latitude,longitude` row for that
school. Such schools stay off the map (per the brief) but remain in the ranking.
This is intentional — an older version fell back to `"<town>, Polska"` and
silently planted 773 of 1,720 schools on their town's centroid (351 alone landed
on Pałac Kultury in Warsaw). On the current data **3 of 12,889 schools** have no
coordinates.

### The gate does not retro-fit the cache

The gate runs when a coordinate is **written**, so a row already in
`school_coords.csv` is never re-tested: `plan_geocoding` keeps any cached row
whose address is unchanged. Stale rows from an older geocoder therefore survive
until they are re-fetched. Zero their lat/lon and re-run, or re-run with
`--force`, and the gate will hold on the rewrite. Check Q in
`validate_export.py` is what tells you which rows need it.

Run the script after adding new schools, then re-run the notebook's export
cells so the fresh coordinates land in `schools-index.json`.

### Post-run reports (always emitted)

After every run (including `--report-only`, which skips geocoding), the
script writes / refreshes:

- **`data/school_coords_unmapped.csv`** — one row per school with no
  coordinates on file, with `rspo, miejscowosc, ulica_nr` and a ready-made
  `google_maps_search` URL. Its rows come from the **school population**, not
  from the cache: a school the cache has no row for at all has nothing for a
  cache-driven filter to catch, so it used to fall out of the triage list as
  well as off the map. The file is the manual-triage list:
  open the URL, find the school, paste the coords into `school_coords.csv`
  by hand. If everything mapped, the file has only a header.
- **Rejected-geotag list** to stdout, after a geocoding run (not
  `--report-only`, which has no run to report). Names every school whose RSPO
  geotag was dropped for falling outside its own voivodeship. See the gate
  above — these are worth investigating, not filtering.
- **Shared-coord warning** to stdout. Lists any group of `SHARED_COORD_WARN_THRESHOLD`
  (default 3) or more schools sitting on the same `(lat, lon)`. With the
  no-centroid-fallback rule, this should never happen except for genuine
  multi-school complexes (rare). A flagged group means either a real
  campus or a regression in the geocoder — eyeball it.

Run `uv run python scripts/geocode_schools.py --report-only` to regenerate
these reports against the existing cache without doing any geocoding.

---

## Frontend / map app

The map application is a **separate concern** from this notebook. Its complete
build specification — UI, filters, popups, data-loading strategy, colour
computation, internationalisation, build order — lives in **`MAP_APP_BRIEF.md`**
(repo root). That is the single source of truth for the frontend; do not
duplicate its UX decisions here.

What this notebook guarantees the frontend can rely on (the export contract):

- `schools-index.json` carries, as parallel arrays: `rspo`, `name`, `teryt`,
  `powiat`, `miejscowosc`, `ulica_nr`, `is_public` ("Tak"/"Nie"), `n_years`,
  `lat`/`lon` (nullable), `on_map`. Identity only — no scores.
- `scale.json` carries `school[level][metric][subject] = {sigma, sigma_centre,
  p1, p99}` and `metadata.slider_ranges[level][metric]`, for all four reference
  levels.
- `regions-{level}.json` × 3 carry the aggregate `score` / `rank` / `pct` /
  `n_ranked` per (metric, subject), plus `parent`, `n_schools`, `n_students`,
  `lat`/`lon` and this level's own `sigma` / `sigma_centre`. `score` is `null`
  where a suppression rule withheld it, and the frontend must say **which** rule
  — no schools, only child, or too small a reference — not a single "too small".
- `powiat/{teryt4}-{metric}.json` carries, per school/level/subject, the `base`,
  `loo`, `single_year` and `last_k` views (see the Export section above).
- These fields exist specifically to support the frontend's needs (metric,
  subject, reference-level and zoom-level switching; value filtering;
  public/private filtering; per-level colouring; uncertainty ranges). If you
  change the export, keep them — or update `MAP_APP_BRIEF.md` in lockstep.

Two principles set here because they constrain the **data/metric**, not just the
UI, and must survive any frontend rewrite:

- **Outcome, not value-added** (a specific instance of the global "Causal claims
  — only what the data can support" rule). The metric measures exam outcomes. We
  have no student-intake data, so the data cannot establish *why* schools or
  groups differ. Describe the pattern (e.g. in the public/private filter) without
  naming a cause.
- **Never invent coordinates.** If geocoding fails, `lat`/`lon` stay `null` and
  the school is omitted from the map. Never substitute approximate coordinates.

## Global coding rules (apply everywhere)

- **Exact column names** — never substring-match (`df[f'mean_{s}']`, not
  `next(c for c in cols if 'mean' in c)`).
- **`pd.to_numeric(errors='raise')`** by default; use `'coerce'` only when
  non-numeric values are expected, and then assert/log how many were coerced.
- **Log dropped rows** with counts; never silently filter.
- **Assertions** for structural assumptions (e.g. RSPO unique per school name).
- **No `try/except`** to suppress errors during data loading.
- **Explore before analysing** — check shape/dtypes, `value_counts(dropna=False)`
  on key columns, cross-check related columns (if `n_students > 0`, verify
  mean/median are non-null); stop and report on unexpected nulls.

### Polish characters

Always preserve Polish characters literally in output: use
`json.dump(..., ensure_ascii=False)` and write files with `encoding='utf-8'`.

### Notebook outputs

Always **execute the notebook and embed outputs** (charts, tables) so results are
visible without re-running. Use `--ExecutePreprocessor.record_timing=False` to
keep the diff clean — without it nbconvert injects per-cell `execution` metadata
(timestamps for `iopub.execute_input`, `iopub.status.busy`, etc.) that change
every run:

```bash
cd notebooks
uv run jupyter nbconvert \
    --to notebook --execute --inplace \
    --ExecutePreprocessor.record_timing=False \
    how_to_measure_school_quality.ipynb
```

Run from the project root's `notebooks/` dir so the relative paths
(`../data`, `../output`) resolve. Warn before executing if long-running cells
changed (the export reads/writes several MB of xlsx).

If you forgot the flag and want to clean an already-recorded notebook, this
one-liner strips the metadata:

```bash
uv run python -c "
import json, pathlib
p = pathlib.Path('notebooks/how_to_measure_school_quality.ipynb')
nb = json.loads(p.read_text())
for c in nb['cells']:
    c.get('metadata', {}).pop('execution', None)
p.write_text(json.dumps(nb, indent=1, ensure_ascii=False) + '\n')
"
```

---

## Before committing

Both gates must be green. Every commit, no exceptions.

```bash
uv run pytest
```

```bash
uv run ruff check .
```

- **All tests pass.** `tests/` covers the pure functions in `src/school_quality/`,
  which the notebook imports. A failing test is a blocker, not a note for later —
  if it is failing because the expected behaviour genuinely changed, update the
  test deliberately and say so in the commit message.
- **No linter findings.** Fix the code. Do **not** reach for a `# noqa` or a new
  entry in `[tool.ruff.lint] ignore` to quieten the check: that list is only for
  rules established to be noise against this codebase, and every entry carries a
  comment recording why it was dismissed. Silencing a real finding to get a green
  check defeats the gate.

If the change touched the pipeline or the data, also run the export validator and
confirm the published artefacts did not move:

```bash
uv run python scripts/validate_export.py
```

```bash
git status --short docs/data output
```

It must exit 0, and that `git status` should print nothing — unless changing the
published numbers was the actual point of the change, in which case the diff is
the thing to review most carefully.

---

## Agent workflow conventions (permission-friendly commands)

Use command forms that don't trigger a permission prompt, so routine work runs
without interruption. These are standing rules — follow them by default.

- **Edit/test/scan logic via a script file, not an inline heredoc.** Write the
  Python to `/tmp/<name>.py`, run it with `uv run python3 /tmp/<name>.py`
  (matches the allowlisted `Bash(uv run *)`), then **delete it when done**
  (`rm /tmp/<name>.py …`, explicit file list). A short inline `uv run python3 -c
  "…"` is fine for a quick read-only check; reserve files for anything
  multi-step or that mutates the notebook. The lifecycle is always
  create → run → remove; don't leave throwaways lying around.
- **`cd` is its own standalone command.** Never `cd X && cmd` (the compound form
  prompts). The working directory persists between Bash calls, so `cd notebooks`
  in one call and the command in the next is enough.
- **One command, not a shell loop or glob expansion.** Prefer `diff -rq dirA dirB`
  over `for f in dir/*; do diff …; done`; prefer `ls dir` over globbing. Loops and
  `*` expansions over many files prompt.
- **Avoid compound `&&` chains and `rm -rf <dir>`.** Split into separate simple
  commands. For cleanup, remove an explicit file list (`rm a b c`), not `rm -rf`
  on a directory.
- **Output-neutrality check after any notebook edit:** the baseline backup lives
  at `/tmp/out_backup_batch/` (`docs_data/` + `output/`). Re-execute, then
  `diff -rq` the live `docs/data` and `output` against it. Back it up **once**;
  after that only diff against it — never re-copy (re-copying would overwrite the
  trusted pre-change snapshot).
- **Never push unless asked.** `git push` is intentionally off the allowlist;
  commit only when requested, push only when explicitly told to at the end.
