# compare-primary-schools-mazowieckie

Analysis of 8th-grade exam (egzamin ósmoklasisty) results for primary schools in
Poland (CIE data), producing the data for an interactive school-quality map.

> **Data scope:** the whole country — all 16 voivodeships, 380 powiats, 2,479
> gminas and ~12.9k schools. A school's score is its distance from the mean of a
> **reference level**: the country, its voivodeship, its powiat or its gmina. All
> four are exported and the map's "reference point" control picks between them.
>
> The repository keeps the name it was given for its original single-voivodeship
> scope; renaming it is deliberately out of scope.

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
             fetch_geometry.py   — download the PRG boundary polygons into docs/geo/
             geocode_schools.py  — resolve coordinates (RSPO first, Nominatim as fallback)
             validate_export.py  — check the JSON exports against the source xlsx
data/        input data (CIE xlsx files, national) + coordinate cache
output/      xlsx files for analysts
docs/        the map app (GitHub Pages); docs/data/ holds the JSON the app loads
             and docs/geo/ the boundary polygons it draws
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

2. **Refresh the boundary polygons** — only when the TERYT vintage changes
   (gminas are created and merged on 1 January, so which polygons exist depends
   on the vintage). They are committed rather than fetched at run time:

   ```bash
   uv run python scripts/fetch_geometry.py
   ```

   This writes `docs/geo/kraj.json`, `docs/geo/woj/*.json` (16) and
   `docs/geo/pow/*.json` (380). The notebook takes region identity from these
   polygons, not from the schools, so a gmina with no scored school is still a
   real row. Record the vintage in `SOURCES.csv` the way the exam editions are.
   `--dry-run` lists what would be written.

3. **Run the notebook** end to end:

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

4. **Resolve coordinates for new schools** (they are not in the exam data):

   Each school's coordinates are looked up in the **RSPO** register by its school
   id, which needs no contact and is immune to the address column's 2026
   degradation. Only a school with no usable RSPO geotag falls through to
   Nominatim address geocoding.

   Nominatim requires a User-Agent that identifies the application **and** a way to
   contact whoever runs it — requests without a valid contact are rejected. The
   contact is **not** stored in this repo; you supply your own at runtime via the
   `NOMINATIM_CONTACT` environment variable (an email or a URL). Without it the
   script warns and skips the fallback; the RSPO lookups still run:

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

   The script reads `docs/data/schools-index.json` and writes the result to
   `data/school_coords.csv`. Schools already cached with an unchanged address keep
   their coordinates. The Nominatim fallback is rate-limited to ~1 request/second,
   so run this only when new schools appear. Every coordinate — from either route
   — must fall inside the voivodeship the exam data assigns the school; one that
   does not is dropped rather than written. Schools left without coordinates are
   listed in `data/school_coords_unmapped.csv` for manual triage and stay off the
   map but in the ranking — coordinates are never invented, and never guessed at
   the wrong end of the country.

5. **Re-run the export cells** (or the whole notebook) so the fresh coordinates
   are merged into `schools-index.json`.

6. **Validate the export** against the source data before committing:

   ```bash
   uv run python scripts/validate_export.py
   ```

   It independently re-reads the source xlsx and recomputes every per-year metric
   *at all four reference levels*, every view aggregate (base / single-year /
   leave-one-out / last-k), `composite_min`, every rank and percentile, every
   region aggregate and the colour-scale metadata, then checks the JSON the app
   serves — `schools-index.json`, `scale.json`, `regions-{level}.json` and the
   per-powiat shards — against it. It also checks the suppression gates, the
   geometry/region key join, and that no NaN reached the JSON. It never imports
   `src/school_quality`: that independence is what makes it a check rather than a
   restatement. Exit code 0 = everything matches; a non-zero exit with a per-check
   `FAIL` listing means the export and the source disagree — investigate before
   publishing. (Override the locations with `--data-dir` / `--docs-data`.)

7. **Preview the map locally** before committing (see below) — the JSON is only
   as good as it looks on the map.

8. **Commit** the generated files in `docs/data/` and `output/`, plus
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

- `docs/data/schools-index.json` — one entry per school: identity, address,
  coordinates, `n_years`. Loaded when the map opens (~1.8 MB raw).
- `docs/data/scale.json` — the colour-scale anchors (σ, centre, p1/p99) and the
  value-filter slider ranges, keyed by reference level, metric and subject.
- `docs/data/regions-{level}.json` × 3 — one row per voivodeship / powiat / gmina:
  the aggregate score, national rank, within-parent percentile and school counts
  the choropleth colours. Row identity comes from the polygons, so a region with
  no scored school is a real row with `n_schools = 0`.
- `docs/data/powiat/{teryt4}-{metric}.json` × 4 × 380 — the per-school views
  (base, leave-one-out, single-year, last-k) at every reference level the
  metric has — all four for the difference metrics, and just the primary one
  for `mean`/`median`, which do not vary by level (`metadata.levels` says
  which). Fetched one powiat at a time. Median 0.14 MB, largest 4.3 MB
  (Warszawa).
- `docs/geo/` — the PRG boundary polygons (`kraj.json`, `woj/`, `pow/`) the map
  draws, committed rather than fetched at run time.

For analysts (Excel, in `output/`):

- `output/schools-{metric}.xlsx` × 4 — long format (one row = one data point),
  with a `legend` sheet and administrative metadata (powiat, gmina, typ_gminy)
  for filtering and pivot tables
- `output/rejected_schools.{xlsx,csv}` — (school, year) rows dropped for missing
  core-subject results, with which subjects were missing
- `output/rejected_addresses.csv` — address updates the pipeline declined because
  the newer file gave a shortened form of an address it already had (the 2026
  file drops `ul.` and the leading words of street names), with the words that
  would have been lost

Metrics: `mean`, `median`, `diff_mean`, `unit_norm_diff_mean` (default).

## The metric

The primary metric is `unit_norm_diff_mean`: the difference between a school's
mean and its **reference level's** mean in a given year, normalised to the range
[−1, +1], averaged across years weighted by the number of students. Chosen via
leave-one-out stability testing among 8 metrics and 5 aggregation methods.

The map shows `composite_min` by default — the minimum of the three subject scores
(Polish, Maths, English), answering "is the school weak in any subject?". The app
lets you switch the view to a single subject.

Above the school zoom the map colours regions instead, and a region is scored
against **the level above it** — a voivodeship nationally, a powiat within its
voivodeship, a gmina within its powiat. Scoring every level against the
voivodeship would put all sixteen voivodeships at ~0 by construction and flatten
the national view at exactly the zoom where contrast is the point.

For methodological and technical details, see `CLAUDE.md`.
