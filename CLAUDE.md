# CLAUDE.md — Technical reference for the school quality project

This file is the technical reference for an AI agent (Claude Code) working on this
project. It documents the data, conventions, methodology decisions, and output
formats. For a human-facing overview, see `README.md`.

---

## What this project does

Analyses results of the Polish 8th-grade exam (**egzamin ósmoklasisty**) published
by the **Warsaw OKE district** and produces data for an external school-quality map.

**Critical scope fact:** the OKE Warszawa data covers **only the Mazowieckie
voivodeship** (1,663–1,720 schools depending on year), *not* all of Poland.
Always use **"voivodeship"** rather than "national" in code, comments, variable
names, chart labels, and markdown. For example: `voivodeship_mean`, not
`national_mean`; "Voivodeship median per year", not "National median per year".

If the data is ever extended to other OKE districts, the metric is still
well-defined per voivodeship, but the reference-computing functions should be
made parametric over the grouping level.

---

## Repository layout

```
compare-primary-schools-mazowieckie/
├── notebooks/
│   └── how_to_measure_school_quality.ipynb   # the analysis + export (run end to end)
├── scripts/
│   ├── fetch_sources.py                        # download the pinned CIE source files
│   ├── geocode_schools.py                      # geocode addresses → data/school_coords.csv
│   └── validate_export.py                      # check the JSON exports against the source xlsx
├── data/                                       # INPUT (read-only source data)
│   ├── egzamin-osmoklasisty/                   # CIE xlsx files, scoped to Mazowieckie, one per year
│   │   ├── 2021 - E8_2021_szkoly_07.xlsx
│   │   ├── 2022 - E8_2022_szkoly_09.xlsx
│   │   ├── ...
│   │   └── SOURCES.csv                          # provenance of each xlsx (see below)
│   └── school_coords.csv                       # geocoding cache (rspo, address, lat, lon)
├── output/                                     # OUTPUT for analysts (xlsx)
│   └── schools-{metric}.xlsx   × 4
├── docs/                                       # the map app (GitHub Pages serves this)
│   ├── index.html, app.js, style.css           # the frontend (see MAP_APP_BRIEF.md)
│   └── data/                                   # JSON consumed by the app (notebook writes here)
│       ├── schools-base.json
│       └── schools-{metric}.json   × 4
├── README.md
├── CLAUDE.md
└── MAP_APP_BRIEF.md                            # build spec for the map app (frontend)
```

The notebook writes the **JSON** files (for the map) into `docs/data/` and the
**xlsx** files (for analysts) into `output/`. This avoids a copy step: the data
the app serves is generated straight into the directory GitHub Pages publishes.
`school_coords.csv` (geocoding cache) stays in `data/`.

`data/` and `output/` are both singular mass nouns (input data / output data),
paralleling each other. `notebooks/` and `scripts/` are plural (countable files).

---

## Source data format

Each OKE xlsx has a sheet named `SAS` with a two-level header. After loading and
normalising (lowercase, strip Polish diacritics, collapse whitespace), the
relevant columns are:

**Metadata columns** (level-0 group is blank / "meta"):
- `rspo` — unique school identifier (stable across years)
- `nazwa szkoly` — school name
- `czy publiczna` — public/private flag
- `powiat - nazwa`, `gmina - nazwa`, `typ gminy` — administrative geography
- `miejscowosc`, `ulica nr` — address (used for geocoding)
- `wojewodztwo - nazwa` — always "Mazowieckie" (sanity-check this)

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
126/128` → `Mickiewicza 126/128`. Of 1,625 schools present in both 2025 and 2026,
850 are unchanged, 566 lost only the prefix, 196 lost leading words (10 of those
lost a *number*, which makes the address wrong), and 13 genuinely moved.

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
- `gmina`, `powiat`, `typ_gminy`, `miejscowosc`, `ulica_nr`
- Per subject `s`: `n_{s}`, `mean_{s}`, `median_{s}`
  (e.g. `n_polski`, `mean_matematyka`, `median_angielski`)

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
diff_mean_year(school, subject, year) =
    school_mean(subject, year) − voivodeship_mean(subject, year)

unit_norm_diff_mean_year =
    diff_mean_year / (100 − voivodeship_mean)   if diff_mean_year ≥ 0
    diff_mean_year / voivodeship_mean           if diff_mean_year < 0
```

Range [−1, +1]: 0 = at the voivodeship mean, +1 = at the ceiling (100%),
−1 = at the floor (0%). In practice values rarely exceed ±0.5.

`voivodeship_mean(subject, year)` = mean of all schools' `mean_{subject}` in that
year (a per-year reference that neutralises exam-difficulty drift — e.g. the
Maths voivodeship mean jumped ~14 pp between 2021 and 2022).

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
aggregation methods. `unit_norm_diff_mean` + weighted-mean-by-n wins across all
subjects and school sizes ≥ 10 students, and the result holds on the larger
2022-onward population (1,297 schools, including small schools that started
reporting after 2021).

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
(metric, subject)**:

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

A **gradient toggle** (map "Ustawienia", default off; ranking class column always
gradient) renders a continuous colour instead of 3 flat ones: B stays flat
yellow (muddy middle, §7), A ramps yellow→green and C ramps yellow→red out to the
**1st / 99th percentile** of the actual score distribution (robust to outliers,
so one extreme school can't stretch the scale). p1/p99 are computed **client-side**
from the ~1.7k base scores per (metric, subject) — not exported (cheap: a sort of
~1.7k numbers, cached per metric/subject). Only `sigma`/`sigma_centre` come from
the JSON metadata; the ±0.33σ boundary and gradient anchors derive from those.

σ and centre are computed **per metric and per subject**, because the metrics
live on different scales (`mean`/`median` are 0–100; `diff_mean` and
`unit_norm_diff_mean` are difference scales). The rules:

- **`mean` and `median`** (raw 0–100 scale): centre = the mean of school scores
  for that subject (the voivodeship average, ≈ 54–69 depending on subject), σ =
  std across schools. Centring at 0 would make no sense — no school scores 0%.
- **`diff_mean` and `unit_norm_diff_mean`** (difference scales): centre = 0 for
  the three subjects (already centred by construction), σ = std across schools.
- **`composite_min`** (any metric): centre = the empirical *mean* of composite_min
  for that metric. composite_min's distribution is shifted left (the minimum of 3
  draws is systematically below each draw), so centring on its own mean gives a
  usable map instead of one where almost everything is red.

All of these (`sigma[metric][subject]`, `sigma_centre[metric][subject]`) are
written into `schools-base.json` → `metadata`, so the frontend can colour the
map for **any** selected metric, not just the primary one.

Indicative `unit_norm_diff_mean` σ (recomputed each run):
polski ≈ 0.192, matematyka ≈ 0.284, angielski ≈ 0.361, composite_min ≈ 0.245.

All four metrics × four subjects are exported, so the user can toggle both the
metric and the subject that colours the map.

### Primary metric vs the app's default — deliberately different

`unit_norm_diff_mean` is the **primary metric**: the one the LOO stability test
picked, and the one `metadata.default_metric` names in `schools-base.json`.

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
  - within-school year-to-year swing vs voivodeship swing
  - candidate metrics + aggregation methods (joint LOO test, 8 × 5)
  - rank-swing analysis + the density-effect explanation
- **4. Final metric definition** — formulas, why, colour scale; subsection
  "Combining all three subjects" (correlation, composite_min, colour-class counts)
- **5. How school level and rank changes** — base vs LOO vs single-year views;
  lollipop charts for two samples (12 schools = 4 top/4 mid/4 bottom; 15 schools
  = 3 each at P10/30/50/70/90); population-wide scatter of range and min/max
- **6. Export data to external map** — computes alternative views, writes JSON + xlsx

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
  The abbreviation carries nothing the next word doesn't; OKE cleaned this up
  between 2024 and 2026, so collapsing keeps one spelling across years. A lone
  `ul.` is kept — it is the only street-type marker present.

On the 2021–2026 data this declines 745 updates and lets the geocoder touch
**49 schools instead of 790**.

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

- **`docs/data/schools-base.json`** (~3.8 MB raw, ~0.4 MB gzipped — GitHub Pages
  serves gzip) — loaded on map open. Per school: metadata (name, address —
  `miejscowosc`, `ulica_nr`, `gmina`, `powiat` — is_public,
  n_years, lat/lon) plus **base score/rank/pct for ALL four metrics × four
  subjects** under `scores[metric][subject]`. This lets the frontend switch
  metric and filter by value **without** downloading the big per-metric files.
  `lat`/`lon` come from the geocoding cache (`null` if missing). `metadata` holds:
  `default_metric`, `metrics`, `subjects`, `years_in_data`, `sigma[metric][subject]`,
  `sigma_centre[metric][subject]`, and `slider_ranges[metric]` (see below).
- **`docs/data/schools-{metric}.json`** × 4 (~7 MB each, ~0.8 MB gzipped) — all *views* for
  all schools, loaded on demand only when the user opens a school's year-by-year
  history (the map and value-filtering work from base alone). `base` is a flat
  `{score, rank, pct}`; other views are `{param: {score, rank, pct}}` with
  integer-string param keys (`"2021"`, `"2"`).
- **`output/schools-{metric}.xlsx`** × 4 (~6.5 MB each) — long format for analysts, one
  row per (school, subject, view), in two sheets:
  - **`data`** sheet columns: `rspo, school_name, miejscowosc, ulica_nr, powiat,
    gmina, typ_gminy, is_public, n_years, metric, subject, view_kind, view_param,
    score, rank_overall, pct_overall, n_in_view, n_students`.
  - **`legend`** sheet: a human-readable description of the metric, the
    across-years aggregation method, and every column / view_kind / subject — so
    someone validating a school's number knows exactly how it was computed.
- **`output/rejected_addresses.csv`** — every address update the pipeline declined
  as a shortened form: `rspo, school_name, year, kept_miejscowosc, kept_ulica_nr,
  declined_miejscowosc, declined_ulica_nr, dropped_words`. The rejection reason is
  the same for every row, so instead of a constant comment column the report names
  the words that would have been lost (`dropped_words`). Same contract as
  `output/rejected_schools.csv`: whatever the pipeline drops is written out.

### Slider ranges (value filter config)

`metadata.slider_ranges[metric] = {min, max, p1, p99, step}` gives the frontend
the range for the map's "show schools with score above X" filter, per metric
(the scale differs: `mean` is 0–100, `unit_norm_diff_mean` is ≈ −0.85…+0.64).
`p1`/`p99` are robust default slider ends; `min`/`max` are hard limits. The
config is computed at export time (data and config generated together, so they
can't drift) rather than recomputed in the browser.

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

Coordinates are **not** in the OKE data, so they are geocoded separately:

- **Input**: `docs/data/schools-base.json` (rspo + address).
- **Cache**: `data/school_coords.csv` with columns
  `rspo, miejscowosc, ulica_nr, latitude, longitude`.
- **Logic**: if an rspo is in the cache and its address is unchanged, keep the
  cached row **in its original CSV position**; if the address changed, re-geocode
  in place; new schools are **appended at the end**.
- **Geocoder**: Nominatim (OpenStreetMap), 1.1 s between requests, with a
  Mazowieckie-biased multi-strategy lookup (see below).
- **Flags**: `--limit N` (cap new requests, for testing), `--force` (ignore cache).

### Lookup strategy (3 attempts, no centroid fallback)

For each school the geocoder tries in order, and accepts the first result that
falls inside the Mazowieckie bounding box (`lon 19.2–23.2`, `lat 51.0–53.6`):

1. **Structured query** — `street=<clean street>`, `city=<miejscowosc>`,
   `state=województwo mazowieckie`, `country=Polska`, `countrycodes=pl`.
2. **Free-text with viewbox** — `q="<clean street>, <miejscowosc>, województwo
   mazowieckie, Polska"`, `viewbox=` Mazowsza, `bounded=1`.
3. **Free-text with original prefixed street** — same as (2) but keeping the
   original `ul. X` form (some streets disambiguate better with the prefix).

"Clean street" = the original `ulica_nr` with leading `ul./Ul./al./Al./pl./Pl./os./Os.`
stripped. Results that land outside the Mazowieckie bbox are rejected even if
returned (Nominatim's `state=` is sometimes a soft preference).

**The cache key strips the same prefix.** `normalize_address` (used to decide
whether a cached row is still valid) applies `_strip_street_prefix`, so
`ul. Kopernika 5` and `Kopernika 5` are one address. They produce an identical
query, so treating them as different would re-geocode a school for a result that
cannot change — which is exactly what the 2026 file would have triggered for ~570
schools. Keep these two normalisations in step: whatever the query ignores, the
cache key must ignore too.

**No town-only fallback.** If all three strategies fail, the geocoder writes an
empty `latitude,longitude` row for that school. Such schools stay off the map
(per the brief) but remain in the ranking. This is intentional — the previous
version fell back to `"<town>, Polska"` and silently planted 773 of 1,720
schools on their town's centroid (351 schools alone landed on Pałac Kultury
in Warsaw).

### Bbox-validation rule (applies to existing cache too)

A row with `latitude/longitude` outside the Mazowieckie bbox is treated as
invalid. If you spot any in `school_coords.csv` (e.g. due to a stale entry from
an older geocoder), zero its lat/lon and re-run the script — the rule will hold
on the rewrite.

Run the script after adding new schools, then re-run the notebook's export
cells so the fresh coordinates land in `schools-base.json`.

### Post-run reports (always emitted)

After every run (including `--report-only`, which skips geocoding), the
script writes / refreshes:

- **`data/school_coords_unmapped.csv`** — one row per school the geocoder
  could not pin to a street, with `rspo, miejscowosc, ulica_nr` and a
  ready-made `google_maps_search` URL. The file is the manual-triage list:
  open the URL, find the school, paste the coords into `school_coords.csv`
  by hand. If everything mapped, the file has only a header.
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

- `schools-base.json` carries, per school: `rspo`, `name`, `is_public`
  ("Tak"/"Nie"), `n_years`, `miejscowosc`, `ulica_nr`, `gmina`, `powiat`,
  `lat`/`lon` (nullable),
  and `scores[metric][subject] = {score, rank, pct}` for all 4 metrics × 4
  subjects. `metadata` carries `sigma[metric][subject]`,
  `sigma_centre[metric][subject]`, and `slider_ranges[metric]`.
- `schools-{metric}.json` carries, per school/subject, the `base`, `loo`,
  `single_year`, and `last_k` views (see the Export section above).
- These fields exist specifically to support the frontend's needs (metric/subject
  toggles, value filtering, public/private filtering, per-metric colouring,
  uncertainty ranges). If you change the export, keep them — or update
  `MAP_APP_BRIEF.md` in lockstep.

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
