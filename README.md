# compare-primary-schools-mazowieckie

Analysis of 8th-grade exam (egzamin ósmoklasisty) results for primary schools in
the Mazowieckie voivodeship (Warsaw OKE district data), producing the data for an
interactive school-quality map.

> **Data scope:** the Warsaw OKE data covers **only the Mazowieckie voivodeship**,
> not all of Poland. All references ("voivodeship mean") are within this voivodeship.

## Getting started (uv)

1. Install `uv` (once, globally).
2. In the project directory run:

```bash
uv venv
uv sync
uv run jupyter lab
```

`uv` creates a local environment in `.venv`, so the libraries don't conflict with
other projects.

## Project layout

```
notebooks/   analysis (how_to_measure_school_quality.ipynb)
scripts/     fetch_sources.py    — download the pinned CIE source files
             geocode_schools.py  — geocode school addresses
             validate_export.py  — check the JSON exports against the source xlsx
data/        input data (CIE xlsx files, scoped to Mazowieckie) + coordinate cache
output/      xlsx files for analysts
docs/        the map app (GitHub Pages); docs/data/ holds the JSON the app loads
```

## How to regenerate the map data

After a new year of results is published:

1. **Fetch the new xlsx** from the CIE manifest into `data/egzamin-osmoklasisty/`:

   ```bash
   uv run python scripts/fetch_sources.py
   ```

   It resolves the pinned edition for each year in `EDITION_PINS`
   (`src/school_quality/sources.py`) against the CIE manifest and downloads
   whatever isn't already on disk. Existing files are left alone unless
   `--force` is given.

2. **Run the notebook** end to end:

   ```bash
   cd notebooks
   uv run jupyter nbconvert \
       --to notebook --execute --inplace \
       --ExecutePreprocessor.record_timing=False \
       how_to_measure_school_quality.ipynb
   ```

   `record_timing=False` keeps the diff clean — without it nbconvert injects
   per-cell execution timestamps that change every run.

   The export cells overwrite files in `output/` **only if the data changed**
   (so git shows no changes on a re-run with unchanged data). Set
   `FORCE_REGENERATE = True` in section 6 to force regeneration.

3. **Geocode new school addresses** (coordinates are not in the OKE data):

   Nominatim requires a User-Agent that identifies the application **and** a way to
   contact whoever runs it — requests without a valid contact are rejected. The
   contact is **not** stored in this repo; you supply your own at runtime via the
   `NOMINATIM_CONTACT` environment variable (an email or a URL):

   ```bash
   # Inline, for a one-off run:
   NOMINATIM_CONTACT=you@example.com uv run python scripts/geocode_schools.py

   # Or set it once for the shell session:
   export NOMINATIM_CONTACT=you@example.com
   uv run python scripts/geocode_schools.py
   ```

   You can also pass it as a flag (`--contact you@example.com`), which overrides the
   env var. If neither is set, the script stops immediately with an explanatory
   error before making any request.

   The script reads `docs/data/schools-base.json`, geocodes new or changed addresses
   via OpenStreetMap (Nominatim), and writes the result to
   `data/school_coords.csv`. Unchanged addresses keep their cached coordinates.
   Geocoding is slow (~1 request/second), so run it only when new schools appear.

4. **Re-run the export cells** (or the whole notebook) so the fresh coordinates
   are merged into `schools-base.json`.

5. **Validate the export** against the source data before committing:

   ```bash
   uv run python scripts/validate_export.py
   ```

   It independently re-reads the OKE xlsx and recomputes every metric, view
   aggregate (base / single-year / leave-one-out / last-k), `composite_min`, and
   rank/percentile, then checks the JSON the app serves
   (`docs/data/schools-base.json` and `schools-{metric}.json`) against it. Exit
   code 0 = everything matches; a non-zero exit with a per-check `FAIL` listing
   means the export and the source disagree — investigate before publishing.
   (Override the locations with `--data-dir` / `--docs-data`.)

6. **Preview the map locally** before committing (see below) — the JSON is only
   as good as it looks on the map.

7. **Commit** the generated files in `docs/data/` and `output/`, plus
   `data/school_coords.csv`.

## Previewing the map locally

`docs/` is a plain static site, but it fetches `docs/data/*.json`, so opening
`index.html` straight from the filesystem fails — browsers block `fetch()` on
`file://`. Serve the directory over HTTP instead:

```bash
uv run python -m http.server 8765 --bind 127.0.0.1 --directory docs
```

Then open:

- <http://localhost:8765/index.html> — the map
- <http://localhost:8765/ranking.html> — the ranking table
- <http://localhost:8765/help.html> — the help page (metric definitions, caveats)

`--bind 127.0.0.1` keeps the server on your own machine; without it Python
listens on every interface and anyone on your network can reach it. Any free
port works — 8765 is just the one this project uses by convention.

**Stopping it.** `Ctrl-C` in the terminal running it. If it is in the background
or you have lost the terminal:

```bash
pkill -f "http.server 8765"
```

To check whether something is already on the port before starting (the server
fails with `Address already in use` if so):

```bash
ss -ltnp | grep 8765
```

Hard refresh (`Ctrl-Shift-R`) after regenerating the data — the browser caches
the JSON, so a normal reload can show you the previous run's numbers.

## Output files

For the map (JSON, written to `docs/data/` so GitHub Pages serves them directly):

- `docs/data/schools-base.json` — metadata + every school's base score/rank/pct
  for all metrics and subjects (loaded immediately when the map opens; ~0.4 MB
  gzipped)
- `docs/data/schools-{metric}.json` × 4 — all score views (base, leave-one-out,
  single-year, last-k) for a given metric (loaded on demand; ~0.8 MB gzipped each)

For analysts (Excel, in `output/`):

- `output/schools-{metric}.xlsx` × 4 — long format (one row = one data point),
  with a `legend` sheet and administrative metadata (powiat, gmina, typ_gminy)
  for filtering and pivot tables
- `output/rejected_schools.{xlsx,csv}` — (school, year) rows dropped for missing
  core-subject results, with which subjects were missing
- `output/rejected_addresses.csv` — address updates the pipeline declined because
  the newer file gave a shortened form of an address it already had (OKE's 2026
  file drops `ul.` and the leading words of street names), with the words that
  would have been lost

Metrics: `mean`, `median`, `diff_mean`, `unit_norm_diff_mean` (default).

## The metric

The primary metric is `unit_norm_diff_mean`: the difference between a school's
mean and the voivodeship mean in a given year, normalised to the range [−1, +1],
averaged across years weighted by the number of students. Chosen via leave-one-out
stability testing among 8 metrics and 5 aggregation methods.

The map shows `composite_min` by default — the minimum of the three subject scores
(Polish, Maths, English), answering "is the school weak in any subject?". The app
lets you switch the view to a single subject.

For methodological and technical details, see `CLAUDE.md`.
