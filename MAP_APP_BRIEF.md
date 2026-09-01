# Build brief — interactive school-quality map

This is a complete, self-contained specification for building the public map
application. It consumes data files produced by the analysis notebook (already
generated, in `output/`). You do **not** need to read the notebook or regenerate
data to build the app — everything about the data shape is described below.

---

## 1. What we're building

A public, static, client-side web map where a parent can look up primary schools
anywhere in Poland and see how they perform on the 8th-grade exam (egzamin
ósmoklasisty), 2021–2026. ~12.9k schools, 16 voivodeships, 380 powiats, 2,479
gminas.

The map is a **zoom ladder**, not a single pin layer. Zoomed out it is a
choropleth of regions — voivodeships, then powiats, then gminas — and only at the
deepest rung does it draw individual schools. A region is always coloured by its
distance from **the level above it**. See §5 for the ladder and §4 for what that
distance means.

- **Hosting:** GitHub Pages, serving a `docs/` directory. No backend, no server,
  no API keys, no build step required at runtime.
- **Tech:** vanilla JavaScript + Leaflet.js + Leaflet.markercluster (from CDN).
  No framework, no bundler. Keep it simple enough to open `docs/index.html` and
  have it work.
- **Tiles:** Carto Positron (muted basemap so the coloured school markers stand
  out). Free, attribution required. Do not use a keyed provider.
- **Audience:** Polish parents, mostly on mobile. Polish UI by default.

The whole point is the **colour** — of a region at the upper rungs, of a school
marker at the deepest one — which encodes a quality score. The map must
communicate uncertainty honestly (see §7): it deliberately does **not** show
numeric rankings on the map itself, and where a number would be dishonest it
shows a neutral fill and says which rule withheld it, never a bare grey.

---

## 2. Data files (already generated, in `docs/data/`)

The analysis notebook writes the JSON files straight into `docs/data/`, so they
are already in place for the app to fetch — no copy step. `docs/geo/` is written
by `scripts/fetch_geometry.py` instead, and committed. Paths below are relative
to `docs/` (where `index.html` lives), so the app fetches them as `data/...` and
`geo/...`.

**Five artifacts**, and the split is the whole loading strategy (§8): identity,
colour anchors and region aggregates are small and load eagerly; the per-school
detail is ~1 GB in total and is fetched one powiat at a time.

### 2a. `data/schools-index.json` — loaded immediately on page open

~2.3 MB raw. **Identity only, no scores.** Stored as **parallel arrays**, not an
array of objects — index `i` is one school in every array:

```json
{
  "metadata": { "generated_at": "2026-..." },
  "schools": {
    "rspo":        [2880, 2890, ...],
    "name":        ["PUBLICZNA SZKOŁA PODSTAWOWA IM. JANA BRZECHWY W SŁUPICY", ...],
    "teryt":       ["1425065", ...],   // 7-char string; keep it a string
    "powiat":      ["1425", ...],      // which shard holds this school's scores
    "miejscowosc": ["Słupica", ...],
    "ulica_nr":    ["84", ...],
    "is_public":   ["Tak", ...],       // "Tak" = public, "Nie" = private/non-public
    "n_years":     [5, ...],
    "lat":         [51.41084, ...],    // null if no coordinates
    "lon":         [21.38722, ...],
    "on_map":      [true, ...]
  }
}
```

Key points:
- 12,889 schools. This backs the "find a school" typeahead, the `?school=` deep
  link and the marker metadata — all without any score download.
- `powiat` is the join key to the shard (§2d). Given an rspo you know exactly
  which one file to fetch.
- `lat`/`lon` may be `null` (neither RSPO nor Nominatim placed the school; 3
  today). Those are **not placed on the map** but still appear in the ranking
  page (§6) marked "not on map".

### 2b. `data/scale.json` — loaded immediately on page open

~10 KB. Everything needed to turn a score into a colour, for every combination
the UI can select:

```json
{
  "metadata": {
    "default_metric": "unit_norm_diff_mean",
    "metrics":  ["mean", "median", "diff_mean", "unit_norm_diff_mean"],
    "subjects": ["polski", "matematyka", "angielski", "composite_min"],
    "years_in_data": [2021, 2022, 2023, 2024, 2025, 2026],
    "slider_ranges": {
      "national":    { "mean": {"min": 6.2, "max": 92.0, "p1": 16.93, "p99": 79.05, "step": 0.858}, ... },
      "voivodeship": { ... }, "powiat": { ... }, "gmina": { ... }
    }
  },
  "school": {
    "national": {
      "mean": { "polski": {"sigma": 8.6513, "sigma_centre": 61.2082, "p1": 25.125, "p99": 80.0}, ... },
      ...
    },
    "voivodeship": { ... }, "powiat": { ... }, "gmina": { ... }
  }
}
```

- Keyed by **reference level first** (§4), then metric, then subject.
- `p1`/`p99` are **exported, not computed in the browser**. They used to be
  derived by sorting every loaded school's score; with per-powiat shards that
  would sort a median of 27 schools and colour the same school differently
  depending which shard loaded first.
- `mean` and `median` have no reference population, so their numbers are
  identical under all four level keys. That is deliberate, not duplication to
  "fix".

### 2c. `data/regions-{level}.json` × 3 — the choropleth, loaded per level on demand

`regions-voivodeship.json` (~13 KB), `regions-powiat.json` (~230 KB),
`regions-gmina.json` (~1.5 MB). Parallel arrays again, one entry per region:

```json
{
  "metadata": {
    "level": "gmina",
    "sigma":        { "unit_norm_diff_mean": {"composite_min": 0.0785, ...}, ... },
    "sigma_centre": { "unit_norm_diff_mean": {"composite_min": -0.0519, ...}, ... },
    "min_reference_n": 5,
    "min_percentile_n": 8
  },
  "regions": {
    "teryt":      ["020101", ...],
    "name":       ["Bolesławiec", ...],
    "parent":     ["0201", ...],       // "" for voivodeships
    "lat":        [51.2, ...], "lon": [15.6, ...],
    "n_schools":  [7, ...],
    "n_students": [412, ...],
    "score":     { "unit_norm_diff_mean": { "composite_min": [0.12, null, ...] }, ... },
    "rank":      { "unit_norm_diff_mean": { "composite_min": [231, null, ...] }, ... },
    "pct":       { "unit_norm_diff_mean": { "composite_min": [64.2, null, ...] }, ... },
    "n_ranked":  { "unit_norm_diff_mean": { "composite_min": 2410 } }
  }
}
```

Key points:
- **Row identity comes from the polygons, not from the schools.** A gmina the
  register knows but no scored school sits in is a real row with
  `n_schools = 0` — not a hole in the map.
- **`sigma`/`sigma_centre` live here, per level**, not in `scale.json`. Region
  aggregates are far less spread out than individual schools, so colouring them
  on the school scale would leave the choropleth almost uniformly yellow.
- `score` is `null` where a suppression rule withheld it. The UI must say
  **which** rule (§5) — a bare grey with no explanation is what makes a
  choropleth feel broken, and a wrong explanation is worse than none.
- `rank` is **national** (among every region of that level with a score;
  `n_ranked` is the denominator, and it is smaller than the row count). `pct` is
  among **siblings** — regions sharing a `parent`. Two different denominators on
  purpose; the column headers must name the scope.

### 2d. `data/powiat/{teryt4}-{metric}.json` — one powiat's schools, on demand

4 metrics × 380 powiats = 1,520 files, ~1.04 GB in total: median 0.53 MB, p90
1.2 MB, largest 8.2 MB (Warszawa, `1465`). **Never load more than the one you
need** — this size is precisely why the data is sharded.

```json
{
  "metadata": { "metric": "unit_norm_diff_mean", "powiat": "1425" },
  "schools": {
    "2880": {
      "national":    { "polski": {...}, "matematyka": {...}, "angielski": {...},
                       "composite_min": {
                         "base":        { "score": -0.18, "rank": 10231, "pct": 34.9 },
                         "loo":         { "2022": {"score": ..., "rank": ..., "pct": ...}, ... },
                         "single_year": { "2022": {...}, "2023": {...}, ... },
                         "last_k":      { "2": {...}, "3": {...} }
                       } },
      "voivodeship": { ... }, "powiat": { ... }, "gmina": { ... }
    },
    ...
  }
}
```

- Keyed `schools[rspo][level][subject][view]` — **reference level before
  subject**.
- Ranks and percentiles inside are **national**, over every school in Poland
  present in that view. The shard is a delivery unit, not a population: never
  present a shard's contents as "the ranking".
- View kinds: **base** (flat `{score, rank, pct}`, all the school's years);
  **loo** (keyed by the *excluded* year, only if ≥ 2 years); **single_year**
  (keyed by year); **last_k** (keyed by k, 2 … n_years − 1). All year/number keys
  are **strings** ("2021", "3").

### 2e. `geo/` — the boundary polygons the choropleth draws

`geo/kraj.json` (16 voivodeship polygons), `geo/woj/{ww}.json` × 16 (each holding
its powiats), `geo/pow/{wwpp}.json` × 380 (each holding its gminas). ~48 MB in
total, so again: fetch only the file for the region in view.

**Each file is named for a region but contains its children.** Features are keyed
by `properties.JPT_KOD_JE`, which is the TERYT code — the same key
`regions-{level}.json` and `schools-index.json` use. There is no file below
gmina: at that rung the map draws school markers instead.

### 2f. `output/schools-{metric}.xlsx` × 4 — NOT used by the app

These live in `output/` (not `docs/`), are for human analysts (Excel), and are
not served by the site. Ignore them in the web app.

---

## 3. Coordinates

`lat`/`lon` are already in `data/schools-index.json` (resolved offline by a
separate Python script). The app does **not** geocode schools.

The offline resolver (see `scripts/geocode_schools.py` and CLAUDE.md "Geocoding")
takes coordinates from the **RSPO school register by school id** first, and only
falls back to Nominatim address geocoding for a school RSPO cannot place. It
**never plants a school on its town's centroid**. If no street-level match inside
Poland is found either, the school keeps `lat`/`lon` = null and stays off the
map. So a missing-coords school is genuinely missing — not "approximately
somewhere in town". Today that is 3 of 12,889.

The app may use a geocoding API for **one thing only**: the **address search box**
(turning a user-typed address into a map location to pan/zoom to). Use Nominatim
(OpenStreetMap), subject to its usage policy — and note these specifics, which
differ from the offline script:

- **Identification is automatic in the browser.** Nominatim requires *either* a
  valid `Referer` *or* a `User-Agent`. JavaScript cannot set `User-Agent` (browsers
  block it), but the browser automatically sends `Referer` (the page URL), which
  satisfies the policy. **So the in-browser search needs no email, no API key, and
  no User-Agent config** — do not hardcode any contact in the frontend. (The
  offline `geocode_schools.py` script is different: it runs server-side, has no
  Referer, so it sets a User-Agent with a contact from an env var. That is the
  script's concern, not the app's.)
- **No auto-complete / search-as-you-type.** Nominatim's policy explicitly forbids
  client-side auto-complete against the public API. The search box must fire **only
  on submit** (Enter key or a "Szukaj" button), exactly one request per submit —
  never one request per keystroke.
- **One request per user action**, end-user-triggered only (which an address search
  is). Display OSM attribution as the policy requires.
- **Bias results to Poland.** Pass `countrycodes=pl` so "Kraków" doesn't lose to
  a Kraków in another country, and a `viewbox` covering **Poland**
  (`14.0,55.0,24.3,48.9` as `left,top,right,bottom` — `POLAND_VIEWBOX` in
  `app.js`, the same extent `scripts/geocode_schools.py` uses) with `bounded=0`
  so the box prefers but does not require results inside it. **Do not bias to one
  voivodeship**: the map covers all sixteen, and a viewbox around one of them
  means a parent in Kraków typing a street name gets a Warsaw result preferred.
- This is a **deliberate** choice to use the public Nominatim API, made here with
  knowledge of its policy — not a default to reach for automatically. If the app's
  search traffic ever grows beyond light/moderate, switch to a self-hosted
  Nominatim or a commercial geocoder.

**Never invent or approximate a school's coordinates.** If `lat`/`lon` is null,
the school is simply absent from the map (see §7).

---

## 4. Metrics and the colour scale

Four metrics, exposed as a toggle. `unit_norm_diff_mean` is the **primary**
metric (what the analysis endorses, and `metadata.default_metric`); the app
**opens on `mean`**, which is a UI decision, not a statistical one — a reader
looking for "56%" concludes an unlabelled decimal near zero is a broken page.
The two are allowed to disagree; the frontend never reads `default_metric`.

| Metric | Scale | Meaning |
|--------|-------|---------|
| `mean` | 0–100 | Raw mean exam score (%). Baseline, easiest to read. **App default.** |
| `median` | 0–100 | Raw median exam score (%). Baseline. |
| `diff_mean` | ≈ −58…+29 | School mean minus its **reference level's** mean, in percentage points. |
| `unit_norm_diff_mean` | −1…+1 | `diff_mean` normalised by ceiling/floor distance. **Primary.** |

`median` and `diff_mean` sit behind an "advanced metrics" toggle
(`BASIC_METRICS` in `app.js`): `diff_mean` ranks identically to
`unit_norm_diff_mean` (Spearman 1.000), and `median` lost the stability test to
`mean`. Both stay fully present in the data.

Four subjects, also a toggle: `polski`, `matematyka`, `angielski`, and
`composite_min` (the minimum of the three subject scores — "is the school weak in
*any* subject?"). `composite_min` is the default subject.

### Reference levels — a third axis the UI exposes

A difference-based score is a distance from the per-year mean of **some**
population, and which population is a user-visible choice: `national`,
`voivodeship`, `powiat` or `gmina`. All four are exported for every school
(§2d), and the map's **"Punkt odniesienia" / "Reference point"** selector picks
between them, defaulting to `voivodeship`.

Three rules the UI must honour, none of them optional:

1. **`mean` and `median` do not vary with it.** They are raw 0–100 scores with no
   reference population. Say so next to the disabled-looking control rather than
   letting a user conclude the app ignored their click.
2. **It applies to the school view only.** Above school zoom the ladder decides
   (§5) — a region is always compared with its parent — so the control is
   disabled there, with a note saying why.
3. **The colour anchors must follow it.** Reading `scale.json` at the wrong level
   silently mis-colours every school; `scaleFor(metric, subject)` in `app.js`
   resolves the level from the shared `baselineLevel` binding so no call site can
   forget.

### Colour computation (client-side)

For a **school**, read `centre` and `sigma` from
`scale.json` → `school[baselineLevel][metric][subject]`. For a **region**, read
them from that level's own `regions-{level}.json` → `metadata.sigma_centre` /
`metadata.sigma`. Map the score to one of **3 classes** (boundary ±0.33σ):

| Class | Condition | Flat colour |
|-------|-----------|-------------|
| **A** above average | score > centre + 0.33σ | `#1a9850` green |
| **B** around average | centre − 0.33σ ≤ score ≤ centre + 0.33σ | `#fde08a` yellow |
| **C** below average | score < centre − 0.33σ | `#d6604d` red |

Legend labels are neutral — "powyżej / w okolicy / poniżej średniej" (above /
around / below average), not "good / weak", so a school just past the ±0.33σ
boundary isn't given a value judgement the data can't support (§7). "Średnia"
alone is avoided for B (clashes with the `mean` metric label "Średnia").

The ±0.33σ band is wider than the multi-year base score's own noise (≈ 0.12σ from
LOO), so the 3 buckets are statistically distinguishable. The earlier 5-class
scheme (extra ±1.5σ "saturated" cutoffs) was dropped — ±1.5σ was arbitrary and
left class A empty for median-angielski (centre + 1.5σ > 100).

**Gradient toggle (Ustawienia / Settings, below the legend; default ON — the
ranking class column is always gradient).** A continuous colour instead of 3 flat
ones: **B stays flat yellow** (muddy middle, §7), **A ramps yellow→green** and
**C ramps yellow→red** out to the **1st / 99th percentile** of the score
distribution (robust — one outlier can't stretch the scale). `colourFor(score,
centre, sigma, p1, p99, gradient)` + `gradient3Colour` (app.js) implement it;
clusters colour by the same function on their mean. p1/p99 come from
`scale.json` (`scoreExtent` → `scaleFor`), **not** from sorting whatever is
loaded — see §2b. State persists like other settings (URL `gradient=0` >
localStorage > default on). The legend shows the 3 classes labelled A/B/C
(matching the ranking's "Klasa" column).

The centre differs by metric **and by level**: for `mean`/`median` it's the
population average (≈ 50–64 by subject), for the diff-based metrics it's 0 for
the three real subjects (centred by construction), and `composite_min` uses its
own empirical mean under every metric — the minimum of three draws sits
systematically below each draw, so centring it at 0 would paint almost everything
red. Never assume 0; read centre/sigma from the file.

### Region colour is an aggregate of scores, not of colours

A region's score is computed from its schools' **scores** and only then coloured.
It is **not** the average of its schools' colours, and the UI must not describe it
that way. Under `diff_mean` and `unit_norm_diff_mean` the aggregate is **weighted
by pupil count**, so a 300-pupil school moves a gmina more than a 30-pupil one;
under `mean` and `median` it is a plain average across the region's schools.

---

## 5. Map view (the main page — `index.html`)

### 5.0 The zoom ladder — the structure everything else hangs off

Four rungs, chosen by zoom (`ZOOM_THRESHOLDS`, mirrored in
`src/school_quality/zoom.py` so Python and JS cannot drift):

| Zoom | Rung | What is drawn |
|------|------|---------------|
| < 8 | `country` | the 16 **voivodeship** polygons |
| 8–9 | `voivodeship` | the focused voivodeship's **powiat** polygons |
| 10–11 | `powiat` | the focused powiat's **gmina** polygons |
| ≥ 12 | `gmina` | **school markers**, for the focused powiat |

Two rules are not tunable even though the thresholds are:

- **A region is scored against the level above it.** A voivodeship nationally, a
  powiat within its voivodeship, a gmina within its powiat. Scoring a level
  against itself puts every one of its regions at ~0 by construction — all
  sixteen voivodeships identical, the national view uniformly flat, at exactly
  the zoom where contrast is the point.
- **A click never lands below the rung it drilled into.** A plain `fitBounds` on
  a large region can settle one rung out (measured at 960×679: 2 of the 16
  voivodeships and 14 powiats fit only at zoom 7), which
  redraws the parent choropleth and resets the breadcrumb — clicking a region
  puts you back where you started. Clamp to the rung's floor, computing the
  target zoom with `getBoundsZoom` *before* moving. For a ring-shaped region
  whose centre lies in its own hole, fall back to plain `fitBounds` rather than
  focusing the enclosed city.

**Breadcrumb** above the map (Polska › województwo › powiat › gmina) shows where
you are and steps back out. Focus is derived by point-in-polygon from the map
centre.

**Neutral fill, with a reason.** Where `score` is `null`, colour the region
neutral **and say which rule withheld it** — the three are different facts and a
single "too small" is wrong for most of them:

| Condition | Message |
|---|---|
| `n_schools === 0` | no schools with exam results |
| the region is its parent's only child | only unit in its parent — nothing to compare against |
| otherwise | reference group too small to score against |

- **Layout:** full-screen map + a side panel (school search/list). On mobile the
  panel collapses to a drawer or bottom sheet. **Mobile must work well** — most
  users are on phones.
- **Top nav:** link to the ranking page (`ranking.html`). Carry the current
  metric/subject/language through the link as URL params (§9) so switching pages
  preserves the user's selection.
- **Initial view:** Poland, `[52.0, 19.2]` at zoom 6, `minZoom: 5`. Not
  fit-to-bounds over the markers: no markers exist until the viewport resolves to
  a powiat, and the choropleth needs a viewport before it can decide what is in
  focus. It is also where the breadcrumb's "Polska" root returns to — there is no
  country polygon to fit against.
- **Markers:** fixed-size coloured circles, colour from §4, drawn **only at the
  deepest rung** and only for the focused powiat (a median of 27 schools, up to
  377). Cluster them (Leaflet.markercluster) and do **not** size markers by
  student count.
- **Cluster colour:** the cluster's circle is coloured by the **mean of its
  children's scores** for the currently-selected (metric, subject), mapped
  through the same 3-class scale from §4. This lets the user see "this cluster is
  broadly red / green" without expanding it. Note this is intentionally a mean of
  `composite_min` values when that subject is selected — conceptually a "mean of
  mins", which is fine for a quick local read. It is **not** how a region's
  colour is computed (§4): clusters are a marker-rendering convenience, region
  aggregates are exported data.
- **No numeric ranks on the map.** Colour only. (Rationale in §7.)
- **Toggles:**
  - Subject: Polish / Maths / English / composite_min → recolours markers.
  - Metric: mean / median / diff_mean / unit_norm_diff_mean → recolours markers.
  - Reference point: national / voivodeship / powiat / gmina → recolours markers
    and reloads nothing (all four levels are already in the loaded shard).
    Disabled above the school rung, and for `mean`/`median`, with a note saying
    why in each case (§4).
  - Metric and subject changes recolour from the loaded shard and repaint the
    choropleth from the loaded region file; only a metric change needs a new
    shard fetch.
- **Filters:**
  - **Public / private** (`is_public` == "Tak" / "Nie"). Important — see §7.
  - **Score threshold:** "show only schools scoring above X" for the current
    (metric, subject). Use `slider_ranges[baselineLevel][metric]` for the slider:
    `min`/`max` as hard bounds, `p1`/`p99` as sensible default handle positions,
    `step` as the increment.
    - **Reset the threshold when the user switches metric.** The scales differ
      (`mean` is 10–87, `unit_norm_diff_mean` is −0.85…0.64), so a numeric value
      that filtered out the bottom 1% on one metric is meaningless on another.
      Snap back to the default handle position (`p1` of the new metric).
      Subject changes do *not* reset the threshold — same metric, comparable scale.
  - **Minimum n_years:** hide schools with little history.
- **Zoom + address search:** native Leaflet zoom, plus a search box that
  geocodes a typed address (Nominatim) and pans/zooms there so the user can see
  nearby schools. Fire the geocode **only on submit** (Enter / button), one
  request per submit — no search-as-you-type (Nominatim policy; see §3).
- **Find a school (typeahead):** a *separate* box from the address search,
  searching **our own `schools-index.json`** by name + town as the user types.
  This is **not** geocoding — it's a local substring filter over data already in
  the browser, so the Nominatim "no auto-complete" rule (§3) does **not** apply.
  Matching is diacritic-insensitive (NFD strip + explicit `ł→l`; "slupica" finds
  "Słupica"), capped to 15 results, keyboard-navigable (↑/↓/Enter/Esc). Picking a
  school flies to it and opens its popup (reuses the `?school=` focus path).
  Because the index knows all 12,889 schools but the map holds one powiat's
  markers, picking a school usually means the marker does not exist yet: record
  it as pending and open the popup once that powiat's markers are built.
  Schools without coordinates are listed with a "(not on map)" tag and hand off
  to the ranking (`ranking.html?school=<rspo>`), which selects them.
- **Popup (on marker click):** show the rich base stats this school has —
  name, public/private, town + street, n_years, and for the selected metric the
  per-subject score / rank / pct plus composite_min. Show a warning badge if
  applicable (§7).
  - **Year-by-year history:** a button inside the popup ("Pokaż historię
    roczną") triggers the on-demand load (§8) and then shows, for this school
    under the selected metric:
    - a small **per-subject sparkline** (one line per subject including
      composite_min, x = year, y = score) — quick visual of the trend.
    - a small **table** below the sparkline with rows = years, columns =
      subjects, values = single-year score; final row(s) summarise `last_k`
      (k = 2 … n_years − 1) and `loo` ranges.
    - If this layout reads poorly in practice, the spec will be revised — but
      both forms exist by default so the user sees shape *and* numbers.
  - **Touch popup behaviour (do not regress).** The year-by-year history makes
    the popup tall, which surfaced three mobile bugs — each fix below is load-
    bearing on touch and must survive any rewrite. "Touch" is detected with
    `matchMedia('(pointer: coarse)')`, **not** UA sniffing (`L.Browser.mobile`
    proved unreliable):
    - **`autoPan: false` on touch.** With autoPan on, the popup growing after
      the async history load made Leaflet pan the map under the user's finger,
      which closed the popup right after it opened. Desktop keeps autoPan.
    - **`max-height: 60vh` + internal scroll on `.leaflet-popup-content`.** So a
      tall popup scrolls inside itself instead of running off-screen (needed
      because autoPan is off on touch).
    - **`closeOnClick: false` on touch.** Dragging to scroll a tall popup ends
      in a tap that lands on the map; the map's default close-on-click then shut
      the popup. On touch the popup closes only via its × button (enlarged to a
      32 px tap target on ≤700 px). Switching schools by tapping another marker
      still works. Desktop keeps click-to-close.
    - **`removeOutsideVisibleBounds: false` on the markercluster group.** By
      default markercluster removes markers outside the buffered viewport;
      removing a marker closes its open popup, so panning the map to read a
      popup closed it. With canvas rendering one powiat's markers are cheap to
      keep, so we keep them all and the popup survives a pan.

---

## 6. Ranking page (separate `ranking.html`)

A separate page — **not** a tab inside `index.html`. Reason: it must be
deep-linkable in its own right (you can share a ranking-page URL with filters
applied without the map's state mixed in). The two pages share styling and a
top nav (Mapa / Ranking), and they pass state to each other via URL params and
localStorage (§9).

A sortable, filterable **table** — this is where numeric ranks are allowed (the
map is not). A **level control** picks what is ranked: voivodeships, powiats,
gminas or schools. The four levels are deliberately **not** symmetric, and the
page must say so rather than leaving the user to discover it by clicking.

### 6a. Region levels (voivodeship / powiat / gmina)

Whole-country, straight from `regions-{level}.json` (§2c) — 2.8 / 49 / 284 KB
gzipped, so the twenty best gminas in Poland cost nothing.

Columns: **national rank**, name, parent name, `n_schools`, `n_students`, score,
class, **percentile within the parent**.

- **Rank and percentile use different denominators on purpose.** Rank is among
  every region of that level in Poland with a score (`n_ranked`, which is smaller
  than the row count). The percentile is among **siblings only**, blank where the
  parent has fewer than `min_percentile_n` (8) children — today 1,042 of 2,479
  gminas. Name both scopes in the column headers; a tooltip is not enough.
- **Suppressed regions stay in the table**, carrying their reason (§5) rather
  than being dropped. The table *is* the enumeration of that level's population,
  so an omitted region reads as a region that does not exist. Sort them last
  under every key and direction.
- Region files carry the `base` view only, so the view / view_param / public
  filters are disabled at these levels, with a note saying why.

### 6b. School level

Ranked **within one chosen powiat**, from that powiat's shard (§2d). A national
school table would need every school's scores at every reference level in one
file — the payload the sharding exists to avoid. So **with no powiat chosen the
page shows a prompt**, not one powiat's worth silently presented as "the
ranking".

Per school row, in this column order:
- **National rank** for the selected (metric, subject, view), school name, town,
  street, public/private, n_years, score, class.
- **LOO rank range** — min and max rank across the LOO folds, e.g. "234 (198–267)".
- **Single-year rank range** — min and max rank across single-year views.
- **Gmina, powiat** — placed last (administrative geography is least important,
  so it trails the score/rank/range columns); single-year range stays ahead of
  them as it carries more signal.

**The rank is national even though the list is one powiat.** So the numbers do
not run consecutively down the column — say that in the header help, or every
reader will report it as a bug. There is **no percentile column at school
level**: the useful within-parent scoping that regions get has no counterpart
here, and a national percentile beside a one-powiat list invites a comparison the
list does not support.

This page has **no reference-point control**. It uses the stored/default level —
which the map may have changed — so any help text must name the level *in force*,
never "the selected reference point".

### 6c. Shared controls

- Metric selector and subject selector.
- **View selector**, including **last_k** — this lets the user compare against
  external rankings. For instance, rankingedukacji.pl uses the arithmetic mean of
  the **last 3 years**, which is `metric=mean`, `view=last_k`, `view_param="3"`.
  (Their ranking also includes non-exam factors, so it won't match exactly, but
  the exam-based part is comparable.)
- Public/private filter, name search, column sorting. At region levels the text
  filter matches name and parent; at school level, name, town, street, gmina
  **and** powiat (e.g. "Vizja", "STO", "powiat pruszkowski").

**Schools without coordinates** (lat/lon null) are excluded from the map but
**must still appear** in the ranking page, marked e.g. "📍✗ brak lokalizacji",
since they have valid scores.

---

## 7. Uncertainty communication (important — this is a design principle, not a nicety)

The data has real uncertainty and the UI must not overstate precision.

- **Map shows colour, never a numeric rank.** A school's rank in the dense middle
  of the distribution swings by ~10% of all positions (100+ places) when a single
  year is added or removed — a density artifact, not a real difference. A number
  like "#234" implies precision the data can't support. The middle class (B,
  ±0.33σ) is a flat "muddy" yellow precisely so the muddy middle looks muddy —
  even in gradient mode B stays flat. Numeric ranks live only in the ranking
  page, always shown *with* their LOO/single-year ranges.

- **Public/private filter matters.** Empirically the very top of every subject is
  dominated by private schools, so a parent looking for a strong *public* school
  needs to filter private ones out. **Do not editorialise about why** private
  schools rank high — the metric measures exam *outcomes*, not value added, and we
  have no data on student intake. Do not state or imply that it's due to selection
  (or to better teaching) — we cannot tell. Just provide the filter.

- **Warning badge** in a school's popup if any of these holds (one ⚠️ badge, with
  hover/tap text listing which triggered):
  - `n_years < 3` — short history, limited certainty.
  - LOO score range > 1σ **and** `n_years ≥ 3` — many years but high volatility;
    the score depends a lot on which year is included. (Needs a metric file
    loaded to compute the LOO range; if not loaded yet, you can compute this
    lazily when history is fetched, or skip it until then.)

- **Never invent coordinates** (repeat of §3) — a missing-coords school is off the
  map, not placed approximately.

---

## 8. Data loading strategy

The export totals ~1.1 GB. **Nothing may load "all the data"** — every fetch is
scoped to what the current viewport or selection needs.

- **On page load:** `data/schools-index.json` (~2.3 MB raw) and `data/scale.json`
  (~10 KB). Together they give identity, search, the `?school=` deep link and
  every colour anchor.
- **Per region level, on demand:** `data/regions-{level}.json` when the map (or
  the ranking's level control) first reaches that level. Cached for the session.
- **Per region in view, on demand:** the geometry file for the region being drawn
  — `geo/kraj.json`, `geo/woj/{ww}.json` or `geo/pow/{wwpp}.json` (§2e). Never
  the whole `geo/` tree.
- **Per (powiat, metric), on demand:** `data/powiat/{teryt4}-{metric}.json`, once
  the viewport resolves to a powiat or the user picks one in the ranking. This is
  the big one — median 0.53 MB, up to 8.2 MB — so fetch exactly the one needed
  and cache it.

**Cache the promise, not the resolved value.** `zoomend` and `moveend` both fire
in one interaction, so the same key is routinely requested twice before the first
fetch lands; caching the resolved value lets both requests through, which for a
shard means downloading megabytes twice.

**One exception on rejections.** Geometry fetches must *drop* a failed promise
before rethrowing: they fire from `zoomend`/`moveend`, where panning away and
back **is** the natural retry, and a cached rejection would leave that region
permanently unrendered for the rest of the session. Region and shard fetches fire
rarely and on deliberate action, so they keep their cached rejections.

Browser HTTP caching (ETag) covers repeat visits from the same browser for free;
incognito / a different device re-downloads, which is why the per-powiat scoping
matters even more than it looks.

---

## 9. URL state and persistence

The app's selections (metric, subject, filters, selected school) are shareable
via URL. Visiting the same URL from another browser must reproduce the same view.

### Parameters

**`index.html`** (map):
- `metric` — one of `mean`, `median`, `diff_mean`, `unit_norm_diff_mean`.
- `subject` — one of `polski`, `matematyka`, `angielski`, `composite_min`.
- `baseline` — the reference level: `national`, `voivodeship`, `powiat`, `gmina`.
- `gradient` — `0` to force the 3 flat classes (the param appears only when off,
  since gradient is the default).
- `public` — `tak` (show only public), `nie` (show only private), or omitted (all).
- `threshold` — number; minimum score for the current (metric, subject). Schools
  with score below this are hidden.
- `min_years` — integer; minimum `n_years` to show.
- `school` — rspo (integer); if present, pan to the school, open its popup.
- `lang` — `pl` or `en` (omitted = use stored default).

**`ranking.html`** (table):
- `metric`, `subject` — same as above.
- `level` — `voivodeship`, `powiat`, `gmina` or omitted (`school`, the default).
- `region` — the 4-digit powiat TERYT whose schools are listed; school level only.
- `view` — `base`, `last_k`, `single_year`, or `loo`.
- `view_param` — for non-base views: year (e.g. `2023`) or k (e.g. `3`).
- `public` — same as above.
- `q` — free-text name search query.
- `sort` — column key (e.g. `base_rank`); `dir` — `asc` or `desc`.
- `school` — rspo to highlight/scroll to.
- `lang` — same.

Unknown or out-of-range param values are ignored (fall back to defaults), not
errors — be liberal in what you accept.

### Resolution order (precedence)

On page load, for each setting independently:

1. **URL param wins** if present and valid. The tab is then "sealed" — see below.
2. Otherwise **localStorage** value, if present.
3. Otherwise the **built-in default** (`mean`, `composite_min`, baseline
   `voivodeship`, no filters, `pl`). Note the app's default metric is `mean`
   while the *primary* metric is `unit_norm_diff_mean` — see §4.

After load, the tab writes its resolved state into its URL immediately (via
`history.replaceState`), so a reload of this tab preserves what the user is
looking at — even if localStorage has since changed in another tab.

### Writes

When the user changes a setting:
- Update the URL via `history.replaceState` (not `pushState` — we don't want
  every slider tick to add a history entry).
- Update localStorage with the new value.

### Multi-tab behaviour

- **localStorage is the "fresh visit default", not live shared state.**
- **Do not listen to the `storage` event.** Each tab is sealed after load —
  another tab changing its selection does *not* affect this tab.
- Two tabs can hold different selections simultaneously without conflict
  because each tab's state lives in its own URL.

### What is persisted in localStorage

- `metric`, `subject`, `lang`, `baseline`, `gradient`, `advanced_metrics` — the
  user's preferred view (carries across visits).
- `rank_level`, `rank_region` — the ranking page's own level and powiat.
  Namespaced because the map has no equivalent and must not pick them up.

Filters (`public`, `threshold`, `min_years`, `q`, `sort`) and the selected
school are *not* persisted across visits — they are URL-only. Visiting fresh
should show the unfiltered set so the user isn't confused by an old filter
hiding most schools.

---

## 10. Internationalisation

- **Default language: Polish.** Provide a **toggle to English** (some users are
  English-speaking).
- Translate only **UI labels** (buttons, headers, filter names, metric/subject
  labels, warning text, legend). The data files use English technical keys
  (`mean`, `composite_min`, `view_kind`, etc.) — map them to localised display
  labels in both languages.
- **Do not translate proper nouns:** school names, town names, addresses,
  administrative fields stay in Polish in both languages.
- **Persistence:** the language choice follows the same URL > localStorage >
  default rule as other settings (§9). A `?lang=en` link overrides storage;
  toggling the language updates both URL and localStorage.
- Suggested label mapping (PL / EN):
  - metrics: `mean` → "Średnia" / "Mean", `median` → "Mediana" / "Median",
    `diff_mean` → "Różnica od średniej" / "Difference from mean",
    `unit_norm_diff_mean` → "Wynik znormalizowany" / "Normalised score".
  - subjects: `polski` → "Polski" / "Polish", `matematyka` → "Matematyka" /
    "Maths", `angielski` → "Angielski" / "English", `composite_min` →
    "Najsłabszy przedmiot" / "Weakest subject".

---

## 11. Repository placement

```
compare-primary-schools-mazowieckie/
├── docs/                       # GitHub Pages serves this
│   ├── index.html              # the map page
│   ├── ranking.html            # the ranking page
│   ├── help.html               # the bilingual methodology / help page
│   ├── methodology.html        # redirect stub kept for links shared before the rename
│   ├── app.js                  # shared: loading, colour, URL state, i18n, charts
│   ├── map.js                  # the map page only
│   ├── ranking.js              # the ranking page only
│   ├── help.js                 # the help page only
│   ├── style.css               # shared styles
│   ├── data/                   # the notebook writes these directly — do not edit by hand
│   │   ├── schools-index.json
│   │   ├── scale.json
│   │   ├── regions-voivodeship.json
│   │   ├── regions-powiat.json
│   │   ├── regions-gmina.json
│   │   └── powiat/{teryt4}-{metric}.json   × 4 × 380
│   └── geo/                    # scripts/fetch_geometry.py writes these
│       ├── kraj.json
│       ├── woj/{ww}.json       × 16
│       └── pow/{wwpp}.json     × 380
└── output/                     # xlsx for analysts (not served by the site)
```

The notebook generates the JSON straight into `docs/data/`, so there is no copy
step — the app fetches `data/schools-index.json` etc. relative to `index.html`
(and the same relative paths work from `ranking.html` and `help.html`, since they
sit side-by-side). The pages share `app.js` and `style.css`, so common code
(loading, colour mapping, URL state, i18n, the small line charts) lives in one
place; each page's own logic lives in its own file.

---

## 12. Suggested build order

1. Static page + Leaflet + Carto Positron tiles, load `data/schools-index.json`
   and `data/scale.json`, open on Poland at zoom 6.
2. The zoom ladder (§5.0): `levelForZoom`, point-in-polygon focus, geometry
   fetched per region in view, the choropleth from `regions-{level}.json`.
3. Region tooltips, including the three neutral-fill reasons; the breadcrumb; the
   click-never-lands-below-its-rung clamp.
4. School markers at the deepest rung: fetch the focused powiat's shard, join to
   the index, colour by §4.
5. Subject + metric toggles, then the reference-point selector (recolour from the
   loaded shard; disabled above the school rung and for `mean`/`median`).
6. Popup with base stats + warning badge for `n_years < 3`.
7. Filters: public/private, score threshold (using `slider_ranges[level][metric]`),
   min n_years.
8. Clustering (cluster colour = mean of children) + address search + "find a
   school" typeahead over the index.
9. Ranking page (`ranking.html`): region levels from `regions-{level}.json`
   first — they are whole-country and small — then school level behind a powiat
   picker.
10. URL state & persistence (§9): `history.replaceState` + localStorage on both
    pages, plus the cross-page nav that carries metric/subject/baseline/language.
11. Year-by-year history (sparkline + table) in popups; LOO/single-year ranges and
    the last_k view in the ranking; the LOO-range warning badge.
12. Polish/English toggle.
13. Mobile layout pass.

Build incrementally; steps 1–4 already give a working national choropleth that
drills down to real schools, on ~2.3 MB plus one shard.
