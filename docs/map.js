// Map page: Leaflet + Carto Positron tiles, coloured school markers with
// clustering, filters, address search, popups, URL state + persistence.

(function () {
  // Touch-primary device? `matchMedia('(pointer: coarse)')` reflects whether
  // the main input is a finger, which is more reliable than UA sniffing
  // (L.Browser.mobile). Used to relax popup behaviour on touch (no autoPan,
  // no click-to-close — see createMarker).
  const IS_TOUCH = window.matchMedia && window.matchMedia('(pointer: coarse)').matches;

  // ---------------------------------------------------------------------------
  // State (single source of truth for the page)

  const state = {
    metric: DEFAULTS.metric,
    subject: DEFAULTS.subject,
    publicFilter: 'all',          // 'all' | 'tak' | 'nie'
    threshold: null,              // number; null means no threshold filter
    minYears: 1,                  // 1..(number of dataset years)
    selectedSchool: null,         // rspo (number) or null
    lang: DEFAULTS.lang,
    gradient: true,               // continuous colour by default; flat 3 classes via the toggle
    advancedMetrics: false,       // unlocks median + diff_mean in the metric select
    popupTablesOpen: false,       // year-by-year numbers in the popup; reset per school
  };

  let map = null;
  let clusterGroup = null;
  // The schools currently on the map: one powiat's worth, joined from a shard
  // to schools-index.json. Filled by renderSchools once the viewport resolves
  // to a powiat; empty at every zoom above that, where regions are drawn.
  let loadedSchools = [];
  let markersByRspo = new Map();  // rspo -> Leaflet circleMarker
  let schoolSearchIndex = [];     // [{rspo,name,town,gmina,onMap,hay}] for the "find school" box
  let historyData = null;         // metric-keyed cache, filled in the background
  let historyError = false;       // last background fetch failed

  // ---------------------------------------------------------------------------
  // Initial state resolution: URL > localStorage > default (§9)

  function resolveInitialState() {
    state.advancedMetrics = resolveAdvancedMetrics();
    state.metric   = resolvePref('metric',  METRICS);
    state.subject  = resolvePref('subject', SUBJECTS);
    state.lang     = resolvePref('lang',    ['pl', 'en']);

    const url = getURLParams();
    const pub = url.get('public');
    if (pub === 'tak' || pub === 'nie' || pub === 'all') state.publicFilter = pub;

    const thr = parseFloat(url.get('threshold'));
    state.threshold = Number.isFinite(thr) ? thr : null;

    const my = parseInt(url.get('min_years'), 10);
    if (Number.isInteger(my) && my >= 1) state.minYears = my;  // upper bound clamped after load

    const school = parseInt(url.get('school'), 10);
    state.selectedSchool = Number.isInteger(school) ? school : null;

    // gradient: URL > localStorage > default(true)
    const gradParam = getURLParams().get('gradient');
    if (gradParam === '1') state.gradient = true;
    else if (gradParam === '0') state.gradient = false;
    else { const stored = readPrefs().gradient; state.gradient = stored === undefined ? true : stored; }
  }

  function syncURL() {
    const range = scaleData?.metadata.slider_ranges[baselineLevel][state.metric];
    const thresholdActive = range && state.threshold != null && state.threshold > range.min;
    setURLParams({
      metric:     state.metric  !== DEFAULTS.metric  ? state.metric  : null,
      subject:    state.subject !== DEFAULTS.subject ? state.subject : null,
      public:     state.publicFilter !== 'all' ? state.publicFilter : null,
      threshold:  thresholdActive ? state.threshold : null,
      min_years:  state.minYears > 1 ? state.minYears : null,
      school:     state.selectedSchool,
      lang:       state.lang !== DEFAULTS.lang ? state.lang : null,
      gradient:   state.gradient ? null : '0',   // param only when off (on is the default)
    });
  }

  // ---------------------------------------------------------------------------
  // Map setup

  function initMap() {
    // An explicit opening view on Poland. Nothing else sets one now: the old
    // fitBounds over every marker is gone, and the choropleth needs a viewport
    // before it can resolve which region is in focus.
    map = L.map('map', { zoomControl: true, preferCanvas: true, minZoom: 5 })
      .setView([52.0, 19.2], 6);
    L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png', {
      attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors © <a href="https://carto.com/attributions">CARTO</a>',
      subdomains: 'abcd',
      maxZoom: 19,
    }).addTo(map);
  }

  // ---------------------------------------------------------------------------
  // Markers + clustering

  function createMarker(school) {
    const marker = L.circleMarker([school.lat, school.lon], {
      radius: 7,
      weight: 1,
      color: '#222',
      fillOpacity: 0.85,
    });
    marker._school = school;  // attach for cluster colour access
    marker.on('popupopen', () => {
      state.selectedSchool = school.rspo;
      state.popupTablesOpen = false;   // each newly opened school starts collapsed
      syncURL();
    });
    marker.on('popupclose', () => {
      if (state.selectedSchool === school.rspo) {
        state.selectedSchool = null;
        syncURL();
      }
    });
    // Function-form: re-rendered each time the popup opens, so it reflects the
    // current metric/subject/history without an empty-flash on first open.
    marker.bindPopup(() => renderPopup(school), {
      maxWidth: 360,
      minWidth: 280,
      // Mobile bug fix: the popup grows tall when the background history fetch
      // lands and its chart is added. With autoPan on, Leaflet then pans the map
      // to fit the taller popup — on touch that map movement, landing under the
      // just-tapped finger, was closing the popup right after it opened (worse
      // the longer the school's history, because taller popup = bigger pan).
      // Disable autoPan on touch devices; the max-height + internal scroll
      // (see .leaflet-popup-content in style.css) keeps a tall popup usable
      // without needing to pan. Desktop keeps autoPan — the bug is touch-only
      // and autoPan is genuinely useful there.
      autoPan: !IS_TOUCH,
      // On touch, dragging to scroll a tall popup ends with a tap that lands on
      // the map, and the map's default closePopupOnClick then shuts the popup.
      // Since reading a long history requires that drag, disable click-to-close
      // on touch: the popup closes only via its × button (enlarged for touch in
      // style.css). Switching schools by tapping another marker still works —
      // that's a marker tap, not an empty-map tap. Desktop keeps click-to-close.
      closeOnClick: !IS_TOUCH,
    });
    return marker;
  }

  function scoreOf(school, metric, subject) {
    return school.scores?.[metric]?.[subject]?.score ?? null;
  }

  function colourOfSchool(school, metric, subject) {
    const score = scoreOf(school, metric, subject);
    const { sigma, sigma_centre: centre } = scaleFor(metric, subject);
    const { p1, p99 } = scoreExtent(metric, subject);
    return colourFor(score, centre, sigma, p1, p99, state.gradient);
  }

  function applyMarkerColour(marker) {
    const fill = colourOfSchool(marker._school, state.metric, state.subject);
    marker.setStyle({ fillColor: fill });
  }

  function clusterIcon(cluster) {
    const children = cluster.getAllChildMarkers();
    let sum = 0, n = 0;
    for (const m of children) {
      const sc = scoreOf(m._school, state.metric, state.subject);
      if (sc != null) { sum += sc; n++; }
    }
    const { sigma, sigma_centre: centre } = scaleFor(state.metric, state.subject);
    const { p1, p99 } = scoreExtent(state.metric, state.subject);
    const fill = n > 0 ? colourFor(sum / n, centre, sigma, p1, p99, state.gradient) : COLOURS.missing;
    const count = children.length;
    // Size scales gently with count.
    const size = Math.min(56, 28 + Math.round(Math.sqrt(count) * 2));
    const html = `<div class="school-cluster" style="background:${fill};width:${size}px;height:${size}px;">${count}</div>`;
    return L.divIcon({ html, className: '', iconSize: [size, size] });
  }

  function buildClusterGroup() {
    clusterGroup = L.markerClusterGroup({
      chunkedLoading: true,
      showCoverageOnHover: false,
      // Tighter than the default 80 so Warsaw breaks into multiple clusters
      // by neighbourhood as soon as the user zooms in past the city level,
      // instead of staying one big blob until you zoom to the street level.
      maxClusterRadius: 30,
      // At and beyond city-level zoom every school shows individually — at
      // that point the user is looking for specific schools, not aggregate
      // patterns, so clustering only obscures.
      disableClusteringAtZoom: 14,
      spiderfyOnMaxZoom: false,
      // Keep all markers in the layer even when off-screen. By default
      // markercluster removes markers outside the (buffered) viewport for
      // performance — but removing a marker closes its open popup, so panning
      // the map to read a popup was closing that popup. With canvas rendering
      // (preferCanvas) ~1,700 circle markers cost little, so we keep them all.
      removeOutsideVisibleBounds: false,
      iconCreateFunction: clusterIcon,
    });
    map.addLayer(clusterGroup);
  }

  // What renderSchools last put on the map, or null when the markers have been
  // torn down. renderLevel's region branch resets it, so zooming out and back
  // into the same powiat rebuilds rather than leaving the map empty.
  let renderedSchoolsKey = null;

  // Markers for one powiat, joined from its shard to schools-index.json for the
  // identity fields. `powiat` is a 4-DIGIT TERYT — the shard's key.
  //
  // Whole-powiat rather than whole-gmina is deliberate: the shard is a powiat,
  // it is already fetched, and clipping markers at an invisible gmina boundary
  // would read as missing data at the edge of the screen.
  async function renderSchools(powiat) {
    // moveend fires on every pan — including the small autoPan that opening a
    // popup triggers — and rebuilding the markers destroys the popup the user
    // just opened. Panning within one powiat is free.
    //
    // The key holds everything this function reads to build a marker: the shard
    // is per-metric (so a metric change must rebuild — that is what makes
    // onMetricChange's renderLevel() work at this zoom), and baselineLevel
    // picks which block of that shard is read.
    const key = `${powiat}|${state.metric}|${baselineLevel}`;
    if (key === renderedSchoolsKey) return;

    const [shard, index] = await Promise.all([
      loadShard(powiat, state.metric),
      loadIndex(),
      // Not used directly — it primes NAME_CACHE['gmina'] so nameOf() below can
      // resolve a gmina's display name. Cached, so this costs one fetch a session.
      loadRegions('gmina'),
    ]);
    const level = baselineLevel;
    const col = index.schools;
    const pos = new Map(col.rspo.map((r, i) => [String(r), i]));

    loadedSchools = Object.entries(shard.schools).map(([rspo, byLevel]) => {
      const i = pos.get(rspo);
      if (i == null) return null;
      // Never drop a school for having no score at this level — suppression
      // withholds a large share of them at the gmina baseline. A null score
      // colours with COLOURS.missing; dropping it would empty half the map.
      const byMetric = byLevel[level] || {};
      // The shape scoreOf() already expects: school.scores[metric][subject].score
      const scores = { [state.metric]: {} };
      for (const [subject, views] of Object.entries(byMetric)) {
        // `views.base` is legitimately absent for 15,080 cells, all at gmina
        // level: suppression is per view, and `base` spans every year so its
        // minimum reference group is the smallest — a `single_year` view can
        // survive where `base` does not. Assigning `undefined` here is correct
        // and safe: scoreOf reads `scores?.[m]?.[s]?.score ?? null`, so the
        // school colours with COLOURS.missing instead of vanishing. Verified
        // against rspo 133647, whose gmina/polski cell holds only
        // `single_year`. Do NOT rewrite this as `views.base.score` — that is
        // the one form that throws.
        scores[state.metric][subject] = views.base;
      }
      return {
        rspo: Number(rspo),
        name: col.name[i],
        miejscowosc: col.miejscowosc[i],
        ulica_nr: col.ulica_nr[i],
        // A DISPLAY NAME, not a TERYT: `s.gmina` is rendered as "gm. Gózd" in
        // the search typeahead and as a ranking-table column, and is matched
        // against the ranking query. schools-index.json carries no gmina name,
        // so it comes from regions-gmina.json via nameOf — which is why the
        // loadRegions('gmina') above is not optional.
        gmina: nameOf('gmina', col.teryt[i].slice(0, 6)),
        is_public: col.is_public[i],
        n_years: col.n_years[i],
        lat: col.lat[i],
        lon: col.lon[i],
        scores,
      };
    }).filter(Boolean);

    if (regionLayer) { map.removeLayer(regionLayer); regionLayer = null; }
    // buildClusterGroup is not a factory: it assigns clusterGroup and adds
    // itself to the map. One layer for the whole session; refreshFilters does
    // the clearing. Calling it per drill-down would leak a layer each time.
    if (!clusterGroup) buildClusterGroup();
    markersByRspo.clear();
    for (const s of loadedSchools) {
      if (s.lat == null || s.lon == null) continue;
      const marker = createMarker(s);
      applyMarkerColour(marker);
      markersByRspo.set(s.rspo, marker);
    }
    refreshFilters();          // clears the cluster and adds the ones that pass
    renderedSchoolsKey = key;  // set on success only: a failed shard must retry
    // No fitBounds here, unlike the whole-voivodeship plot this replaces: the
    // viewport is what chose this powiat, so refitting would move the map out
    // from under the user mid-zoom and fire another moveend.
  }

  // ---------------------------------------------------------------------------
  // Choropleth: one administrative level per zoom
  //
  // Mirrors src/school_quality/zoom.py — the two are checked against each other
  // in the task's verification step, so keep them in step.

  const ZOOM_THRESHOLDS = [[12, 'gmina'], [10, 'powiat'], [8, 'voivodeship'], [0, 'country']];
  const CHILD_OF = { country: 'voivodeship', voivodeship: 'powiat', powiat: 'gmina', gmina: null };

  function levelForZoom(zoom) {
    for (const [min, level] of ZOOM_THRESHOLDS) if (zoom >= min) return level;
    return 'country';
  }

  let regionLayer = null;

  // The viewport is the single source of truth for what is in focus. Clicking a
  // polygon only fits the map to it and the focus follows, so there is no
  // mutable `focusedRegion` to fall out of step — and no unset state on the
  // ordinary path where the user scroll-zooms in without clicking anything.
  //
  // This is a point-in-polygon test, NOT "nearest published lat/lon". Measured
  // on the committed geometry: 20 of 380 powiat and 110 of 2,479 gmina centres
  // fall OUTSIDE their own polygon, because the ring-shaped `ziemski` powiats
  // and `wiejska` gminas encircle a city — powiat wałbrzyski's centre sits
  // inside Wałbrzych. Nearest-centre would be a coin flip between the one-gmina
  // city and the many-gmina ring around it, fetching different geometry and
  // drawing different children.
  //
  // A level's polygons live in its PARENT's file: kraj.json holds the
  // voivodeships, woj/{teryt2}.json the powiats, pow/{teryt4}.json the gminas.
  // So resolving a level resolves its parent first; every step is cached.
  const PARENT_OF = { voivodeship: 'country', powiat: 'voivodeship', gmina: 'powiat' };
  const KEY_WIDTH = { voivodeship: 2, powiat: 4, gmina: 6 };

  function pointInRing(pt, ring) {
    let inside = false;
    for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
      const xi = ring[i][0], yi = ring[i][1];
      const xj = ring[j][0], yj = ring[j][1];
      if ((yi > pt[1]) !== (yj > pt[1])
          && pt[0] < ((xj - xi) * (pt[1] - yi)) / (yj - yi) + xi) inside = !inside;
    }
    return inside;
  }

  // Rings after the first are holes — a point inside one is outside the polygon.
  function featureContains(feature, pt) {
    const g = feature.geometry;
    const polys = g.type === 'Polygon' ? [g.coordinates] : g.coordinates;
    return polys.some((rings) => pointInRing(pt, rings[0])
      && !rings.slice(1).some((hole) => pointInRing(pt, hole)));
  }

  async function focusFor(level) {
    if (level === 'country') return '';
    const parent = PARENT_OF[level];
    const geo = await loadGeometryFor(parent, await focusFor(parent));
    const c = map.getCenter();
    const pt = [c.lng, c.lat];
    const hit = geo.features.find((f) => featureContains(f, pt));
    if (hit) return hit.properties.JPT_KOD_JE.slice(0, KEY_WIDTH[level]);

    // The centre is over sea or outside Poland — the Baltic at low zoom, or a
    // pan past the border. Fall back to the nearest published centre so the map
    // still resolves to something rather than throwing.
    const regions = await loadRegions(level);
    let best = '', bestD = Infinity;
    regions.regions.teryt.forEach((key, i) => {
      const dLat = regions.regions.lat[i] - c.lat;
      const dLon = regions.regions.lon[i] - c.lng;
      const d = dLat * dLat + dLon * dLon;
      if (d < bestD) { bestD = d; best = key; }
    });
    return best;
  }

  function regionTooltip(regions, i, feature) {
    if (i == null) return escapeHTML(feature.properties.JPT_NAZWA_ || '');
    const r = regions.regions;
    const score = r.score[state.metric][state.subject][i];
    const rank = r.rank[state.metric][state.subject][i];
    const lines = [`<strong>${escapeHTML(r.name[i])}</strong>`];
    if (score == null) {
      // Say WHICH reason: no schools at all, or no usable comparison group. A
      // single grey with no explanation is what makes a choropleth feel broken.
      lines.push(t(r.n_schools[i] === 0 ? 'regionNoSchools' : 'regionTooSmall'));
    } else {
      const n = r.n_ranked[state.metric][state.subject];
      lines.push(`${score.toFixed(2)}${rank == null ? '' : ` (${rank}/${n})`}`);
      lines.push(t('schoolsInRegion', r.n_schools[i]));
    }
    return lines.join('<br>');
  }

  async function renderLevel() {
    const level = levelForZoom(map.getZoom());
    const childLevel = CHILD_OF[level];
    const focused = await focusFor(level);
    // Task 15 defines these two; left commented out so this task's browser
    // check runs without a ReferenceError.
    // renderBreadcrumb(level, focused);
    // syncSelectorAvailability(level);
    if (childLevel === null) { await renderSchools(focused.slice(0, 4)); return; }

    const geo = await loadGeometryFor(level, focused);
    if (childLevel === 'gmina' && geo.features.length === 1) {
      // 66 powiats hold exactly one gmina, where drilling in would draw a single
      // polygon identical to the outline just left. Skip that rung. `focused` is
      // the 4-digit powiat here, which is exactly what renderSchools takes.
      await renderSchools(focused);
      return;
    }

    const regions = await loadRegions(childLevel);
    const idx = new Map(regions.regions.teryt.map((key, i) => [key, i]));
    const width = KEY_WIDTH[childLevel];
    const { sigma, sigma_centre } = regions.metadata;

    if (regionLayer) map.removeLayer(regionLayer);
    // Mirror image of renderSchools' region teardown: without it, zooming out
    // leaves the markers sitting on top of the choropleth.
    if (clusterGroup) {
      clusterGroup.clearLayers();
      markersByRspo.clear();
      loadedSchools = [];
      // Without this the panel keeps claiming "138 of 138 schools" over a map
      // that now has none — refreshFilters is what normally updates it, and
      // this branch clears the cluster itself.
      updateFilterSummary(0);
    }
    renderedSchoolsKey = null;
    regionLayer = L.geoJSON(geo, {
      // colourFor with gradient=false and zero anchors is deliberate: regions
      // use the three flat classes. The continuous ramp stays a school-level
      // affordance, where p1/p99 are meaningful.
      style: (feature) => {
        const key = feature.properties.JPT_KOD_JE.slice(0, width);
        const i = idx.get(key);
        const score = i == null ? null : regions.regions.score[state.metric][state.subject][i];
        return {
          fillColor: score == null ? COLOURS.missing
            : colourFor(score, sigma_centre[state.metric][state.subject],
                        sigma[state.metric][state.subject], 0, 0, false),
          fillOpacity: 0.75, color: '#666', weight: 1,
        };
      },
      onEachFeature: (feature, layer) => {
        const key = feature.properties.JPT_KOD_JE.slice(0, width);
        layer.bindTooltip(regionTooltip(regions, idx.get(key), feature));
        layer.on('click', () => { map.fitBounds(layer.getBounds()); });
      },
    }).addTo(map);
  }

  // ---------------------------------------------------------------------------
  // Filters: figure out which schools pass, push to cluster

  function schoolPassesFilters(school) {
    if (school.lat == null || school.lon == null) return false;
    if (state.publicFilter === 'tak' && !isPublic(school)) return false;
    if (state.publicFilter === 'nie' &&  isPublic(school)) return false;
    if (school.n_years < state.minYears) return false;
    if (state.threshold != null) {
      const sc = scoreOf(school, state.metric, state.subject);
      if (sc == null || sc < state.threshold) return false;
    }
    return true;
  }

  function refreshFilters() {
    clusterGroup.clearLayers();
    const visible = [];
    for (const s of loadedSchools) {
      const marker = markersByRspo.get(s.rspo);
      if (!marker) continue;
      if (schoolPassesFilters(s)) visible.push(marker);
    }
    clusterGroup.addLayers(visible);
    updateFilterSummary(visible.length);
  }

  function updateFilterSummary(nVisible) {
    const total = loadedSchools.filter(s => s.lat != null).length;
    document.getElementById('filter-summary').textContent =
      t('rowsShown', nVisible, total);
  }

  function recolourAll() {
    for (const marker of markersByRspo.values()) applyMarkerColour(marker);
    // Cluster colours redraw when the cluster icons regenerate; force it.
    // Guarded because leaflet.markercluster 1.5.3 throws inside refreshClusters
    // when _topClusterLevel is undefined. That happens because the group builds
    // it in _generateInitialClusters(), called from onAdd via whenReady, which
    // stays deferred while the map has no view. initMap now sets one up front,
    // so that half no longer applies. (An empty group that HAS been added is
    // fine: it returns [] and does not throw.) Still reachable: no markers
    // above powiat zoom, and a filter can still exclude every school in one.
    if (clusterGroup.getLayers().length) clusterGroup.refreshClusters();
  }

  // ---------------------------------------------------------------------------
  // Popup rendering

  function renderPopup(school) {
    const { metric } = state;
    const pub = isPublic(school) ? t('popupPublic') : t('popupPrivate');
    const addr = [school.miejscowosc, school.ulica_nr].filter(Boolean).join(', ');

    const rowsHTML = CORE_SUBJECTS.map(subj => {
      const cell = school.scores[metric]?.[subj];
      return `<tr>
        <th>${t('subject_' + subj)}</th>
        <td class="num">${fmtScore(cell?.score, metric)}</td>
        <td class="num">#${cell?.rank ?? '—'}</td>
        <td class="num">${cell?.pct != null ? cell.pct.toFixed(1) : '—'}</td>
      </tr>`;
    }).join('');

    const composite = school.scores[metric]?.composite_min;
    const compositeHTML = `<tr class="composite-row">
      <th>${t('popupComposite')}</th>
      <td class="num">${fmtScore(composite?.score, metric)}</td>
      <td class="num">#${composite?.rank ?? '—'}</td>
      <td class="num">${composite?.pct != null ? composite.pct.toFixed(1) : '—'}</td>
    </tr>`;

    const warnings = warningsFor(school);
    const warnHTML = warnings.length
      ? `<div class="warning">⚠️ ${warnings.join(' ')}</div>` : '';

    const histHTML = renderHistorySection(school);

    return `
      <div class="popup-school">
        <h3>${escapeHTML(school.name)}</h3>
        <p class="addr">${escapeHTML(addr)} · ${pub} · ${school.n_years} ${t('popupYears')}</p>
        <table>
          <thead><tr><th></th><th>${t('popupScore')}</th><th>${t('popupRank')}</th><th>${t('popupPct')}${helpIconHTML('helpPct')}</th></tr></thead>
          <tbody>${rowsHTML}${compositeHTML}</tbody>
        </table>
        ${warnHTML}
        ${histHTML}
      </div>`;
  }

  function escapeHTML(s) {
    if (s == null) return '';
    return String(s)
      .replaceAll('&', '&amp;')
      .replaceAll('<', '&lt;')
      .replaceAll('>', '&gt;')
      .replaceAll('"', '&quot;');
  }

  function warningsFor(school) {
    const out = [];
    if (school.n_years < 3) out.push(t('warnShortHistory'));
    // LOO-range warning (§7) needs a metric file. Compute lazily once loaded.
    const histPerMetric = historyData?.[state.metric]?.schools?.[String(school.rspo)];
    if (histPerMetric && school.n_years >= 3) {
      const loo = histPerMetric.composite_min?.loo || {};
      const looScores = Object.values(loo).map(v => v?.score).filter(v => v != null);
      if (looScores.length >= 2) {
        const range = Math.max(...looScores) - Math.min(...looScores);
        const { sigma } = scaleFor(state.metric, 'composite_min');
        if (range > sigma) out.push(t('warnVolatile'));
      }
    }
    return out;
  }

  function renderHistorySection(school) {
    if (school.n_years < 2) return '';
    const hist = historyData?.[state.metric]?.schools?.[String(school.rspo)];
    // The per-metric file is fetched in the background from page load, so it is
    // only missing while that fetch is still in flight.
    if (!hist) return `<p class="muted small">${t(historyError ? 'historyFailed' : 'historyLoading')}</p>`;
    return renderHistoryTableAndSparkline(school, hist);
  }

  function renderHistoryTableAndSparkline(school, hist) {
    const years = scaleData.metadata.years_in_data;
    const subjects = ['polski', 'matematyka', 'angielski', 'composite_min'];

    // Build single-year matrix: rows=years (only present), cols=subjects.
    const yearsPresent = years.filter(y => {
      return subjects.some(subj => hist[subj]?.single_year?.[String(y)] != null);
    });

    const tableRows = yearsPresent.map(y => {
      const cells = subjects.map(subj => {
        const v = hist[subj]?.single_year?.[String(y)]?.score;
        return `<td class="num">${fmtScore(v, state.metric)}</td>`;
      }).join('');
      return `<tr><th>${y}</th>${cells}</tr>`;
    }).join('');

    // last_k summary rows (k = 2..n-1)
    const ks = Object.keys(hist[subjects[0]]?.last_k || {}).sort((a, b) => +a - +b);
    const lastKRows = ks.map(k => {
      const cells = subjects.map(subj => {
        const v = hist[subj]?.last_k?.[k]?.score;
        return `<td class="num">${fmtScore(v, state.metric)}</td>`;
      }).join('');
      return `<tr><th>${t('lastKRow', k)}</th>${cells}</tr>`;
    }).join('');

    const header = `<tr><th></th>${subjects.map(s => `<th>${t('subject_' + s)}</th>`).join('')}</tr>`;

    const spark = sparklineSVG(school, hist, yearsPresent);

    // The year-by-year breakdown is a wall of numbers that dominated the popup
    // and read as intimidating rather than informative. The chart above it makes
    // the same point at a glance, so the table hides behind a toggle — matching
    // the ranking's detail panel. The headline table (score/rank/percentile per
    // subject) stays visible: without it the popup would answer nothing.
    const tables = state.popupTablesOpen
      ? `<button type="button" class="popup-tables-toggle">${t('detailHideTables')}</button>
         <table class="history-table">
           <thead>${header}</thead>
           <tbody>${tableRows}${lastKRows ? '<tr class="sep"><td colspan="5"></td></tr>' + lastKRows : ''}</tbody>
         </table>`
      : `<button type="button" class="popup-tables-toggle">${t('detailShowTables')}</button>`;

    return `
      <div class="history">
        ${spark}
        ${tables}
      </div>`;
  }

  function sparklineSVG(school, hist, yearsPresent) {
    if (yearsPresent.length < 2) return '';
    const subjects = ['polski', 'matematyka', 'angielski', 'composite_min'];
    const series = subjects.map(subj => ({
      colour: SUBJECT_COLOURS[subj],
      // composite_min equals the weakest subject each year, so a line would sit
      // exactly on that subject's line and hide it — draw it as dots only.
      pointsOnly: subj === 'composite_min',
      points: Object.fromEntries(
        yearsPresent.map(y => [y, hist[subj]?.single_year?.[String(y)]?.score ?? null])),
    }));
    const svg = lineChartSVG({
      years: yearsPresent,
      series,
      invertY: false,
      fmtY: (v) => fmtScore(v, state.metric),
      width: 260,
      height: 120,
    });
    // Say which of the three score flavours this is: the chart plots each year
    // on its own, while the table above it shows the multi-year score. Without
    // the caption a reader has no way to tell them apart, or to know this is not
    // the LOO view they saw in the ranking.
    return `<div class="sparkline">
      ${helpDetailsHTML('chartYearsCaption', 'helpPopupChart')}
      ${svg}
      <div class="spark-legend">${subjectLegendHTML(subjects)}</div>
    </div>`;
  }

  // ---------------------------------------------------------------------------
  // Threshold slider — re-bound when metric changes (different scale)

  function syncThresholdSlider() {
    const slider = document.getElementById('threshold-slider');
    const display = document.getElementById('threshold-display');
    const range = scaleData.metadata.slider_ranges[baselineLevel][state.metric];
    slider.min = range.min;
    slider.max = range.max;
    slider.step = range.step;
    // No threshold filter on a fresh visit: slider sits at min (= include all).
    // If a URL-provided threshold is in range, honour it; otherwise reset to min.
    let v = state.threshold;
    if (v == null || v < range.min || v > range.max) v = range.min;
    state.threshold = v;
    slider.value = v;
    display.textContent = (v === range.min) ? '—' : fmtScore(v, state.metric);
  }

  async function onMetricChange(newMetric) {
    state.metric = newMetric;
    // Reset threshold when metric changes (scales differ; §5).
    state.threshold = scaleData.metadata.slider_ranges[baselineLevel][newMetric].min;
    writePref('metric', newMetric);
    syncURL();
    syncThresholdSlider();
    recolourAll();
    refreshFilters();
    // recolourAll/refreshFilters only touch markers. The choropleth is painted
    // from the region file's per-metric scores, and at school zoom the markers
    // come from a per-METRIC shard — neither repaints itself, so without this
    // the map silently keeps showing the metric the user just switched away
    // from. renderSchools' key includes state.metric, so it refetches.
    renderLevel();
    // History (sparkline + table) comes from the per-metric file, so switching
    // metric needs the new metric's file. Reopen right away with whatever is
    // cached; the background fetch reopens again once the new file lands.
    if (state.selectedSchool != null) reopenSelectedPopup();
    loadHistoryInBackground();
  }

  function onSubjectChange(newSubject) {
    state.subject = newSubject;
    writePref('subject', newSubject);
    syncURL();
    recolourAll();
    refreshFilters();
    renderLevel();   // repaint the choropleth for the new subject; see onMetricChange
    if (state.selectedSchool != null) reopenSelectedPopup();
  }

  function reopenSelectedPopup() {
    const marker = markersByRspo.get(state.selectedSchool);
    if (marker) marker.setPopupContent(renderPopup(marker._school));
  }

  // ---------------------------------------------------------------------------
  // Address search (Nominatim) — only on submit (§3)

  async function doAddressSearch(query) {
    const url = new URL(NOMINATIM_BASE);
    url.searchParams.set('q', query);
    url.searchParams.set('format', 'json');
    url.searchParams.set('countrycodes', 'pl');
    url.searchParams.set('viewbox', MAZ_VIEWBOX);
    url.searchParams.set('bounded', '0');
    url.searchParams.set('limit', '1');
    const res = await fetch(url.toString(), { headers: { 'Accept-Language': 'pl' } });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const arr = await res.json();
    if (!arr || !arr.length) return null;
    return { lat: parseFloat(arr[0].lat), lon: parseFloat(arr[0].lon) };
  }

  function wireSearch() {
    const form = document.getElementById('search-form');
    const input = document.getElementById('search-input');
    form.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      const q = input.value.trim();
      if (!q) return;
      try {
        const found = await doAddressSearch(q);
        if (!found) { alert(t('searchNotFound')); return; }
        map.setView([found.lat, found.lon], 14);
      } catch (e) {
        console.error(e);
        alert(t('searchError'));
      }
    });
  }

  // ---------------------------------------------------------------------------
  // "Find a school" — local typeahead over our own data (not geocoding)

  // Diacritic-insensitive, lower-cased. NFD strips ą/ć/ę/ó/ś/ź/ż (base + combining
  // mark), but Polish 'ł' is its own codepoint and does NOT decompose, so map it
  // explicitly — otherwise "slupica" would not match "Słupica".
  function normalizeText(s) {
    return (s || '')
      .normalize('NFD')
      .replace(/[\u0300-\u036f]/g, '')
      .replace(/ł/g, 'l').replace(/Ł/g, 'l')   // ł / Ł (no NFD decomposition)
      .toLowerCase();
  }

  // Precompute once after load: the haystack matches on name + town (decision §1).
  function buildSchoolSearchIndex() {
    schoolSearchIndex = loadedSchools.map(s => ({
      rspo:  s.rspo,
      name:  s.name,
      town:  s.miejscowosc || '',
      gmina: s.gmina || '',
      onMap: s.lat != null && s.lon != null,
      hay:   normalizeText(s.name + ' ' + (s.miejscowosc || '')),
    }));
  }

  // On-map school: zoom into its cluster and open the popup. If the school is
  // currently hidden by the filters, just pan to it (no marker in the cluster).
  function goToSchoolOnMap(rspo) {
    const marker = markersByRspo.get(rspo);
    if (!marker) return;
    state.selectedSchool = rspo;
    syncURL();
    if (clusterGroup.hasLayer(marker)) {
      clusterGroup.zoomToShowLayer(marker, () => marker.openPopup());
    } else {
      map.setView(marker.getLatLng(), 14);
    }
  }

  // Off-map school (no coordinates): hand off to the ranking, which selects and
  // scrolls to it. Carry metric/subject/lang so the ranking opens in the same view.
  function goToSchoolInRanking(rspo) {
    const usp = new URLSearchParams();
    if (state.metric  !== DEFAULTS.metric)  usp.set('metric',  state.metric);
    if (state.subject !== DEFAULTS.subject) usp.set('subject', state.subject);
    if (state.lang    !== DEFAULTS.lang)    usp.set('lang',    state.lang);
    usp.set('school', rspo);
    window.location.href = 'ranking.html?' + usp.toString();
  }

  function wireSchoolFind() {
    const input = document.getElementById('school-find-input');
    const list  = document.getElementById('school-find-list');
    let results = [];
    let activeIndex = -1;

    function close() {
      list.hidden = true;
      list.innerHTML = '';
      input.setAttribute('aria-expanded', 'false');
      results = [];
      activeIndex = -1;
    }

    function render() {
      if (!results.length) {
        list.innerHTML = `<li class="ac-empty" aria-disabled="true">${t('findSchoolNoResults')}</li>`;
      } else {
        list.innerHTML = results.map((r, i) => {
          const loc = [r.town, r.gmina && ('gm. ' + r.gmina)].filter(Boolean).join(' · ');
          const tag = r.onMap ? '' : ` <span class="ac-offmap">${t('findSchoolOffMap')}</span>`;
          return `<li role="option" data-i="${i}"${i === activeIndex ? ' class="active"' : ''}>`
            + `<span class="ac-name">${escapeHTML(r.name)}</span>`
            + `<span class="ac-loc">${escapeHTML(loc)}${tag}</span></li>`;
        }).join('');
      }
      list.hidden = false;
      input.setAttribute('aria-expanded', 'true');
    }

    function select(i) {
      const r = results[i];
      if (!r) return;
      input.value = r.name;
      close();
      if (r.onMap) goToSchoolOnMap(r.rspo);
      else goToSchoolInRanking(r.rspo);
    }

    function ensureActiveVisible() {
      const el = list.querySelector('li.active');
      if (el) el.scrollIntoView({ block: 'nearest' });
    }

    input.addEventListener('input', () => {
      const q = normalizeText(input.value.trim());
      if (q.length < 2) { close(); return; }
      results = schoolSearchIndex.filter(s => s.hay.includes(q)).slice(0, 15);
      activeIndex = -1;
      render();
    });

    input.addEventListener('keydown', (e) => {
      if (list.hidden || !results.length) return;
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        activeIndex = Math.min(activeIndex + 1, results.length - 1);
        render(); ensureActiveVisible();
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        activeIndex = Math.max(activeIndex - 1, 0);
        render(); ensureActiveVisible();
      } else if (e.key === 'Enter') {
        e.preventDefault();
        select(activeIndex >= 0 ? activeIndex : 0);
      } else if (e.key === 'Escape') {
        close();
      }
    });

    // mousedown (not click) so the pick fires before the input's blur hides the list.
    list.addEventListener('mousedown', (e) => {
      const li = e.target.closest('li[data-i]');
      if (!li) return;
      e.preventDefault();
      select(parseInt(li.getAttribute('data-i'), 10));
    });

    input.addEventListener('blur', () => setTimeout(close, 120));
  }

  // ---------------------------------------------------------------------------
  // Per-metric history file
  //
  // Fetched for whatever metric is selected — always, in the background, never
  // behind a button. A popup that offered to load its own chart read as a bug,
  // not as a choice.

  async function ensureHistoryLoaded() {
    // No-op until Task 14: history now lives in the per-powiat shards, and
    // loadShard needs a powiat that nothing computes until Task 14's focus
    // resolution lands. Not an oversight — the popup shows "loading" instead.
  }

  // The legend has to follow the gradient toggle, not describe one fixed scheme.
  // With the gradient on, A and C are ramps and the map shows far more than the
  // three colours the legend lists — a reviewer counted five and read it as a
  // mismatch. With it off there are exactly three flat colours, and a legend
  // still showing ramps would be the wrong half of the same problem.
  function syncLegend() {
    const swatchA = document.getElementById('legend-swatch-a');
    const swatchC = document.getElementById('legend-swatch-c');
    const note    = document.getElementById('legend-gradient-note');
    if (state.gradient) {
      swatchA.style.background = `linear-gradient(90deg,${COLOURS.yellow},${COLOURS.satGreen})`;
      swatchC.style.background = `linear-gradient(90deg,${COLOURS.yellow},${COLOURS.satRed})`;
    } else {
      swatchA.style.background = COLOURS.satGreen;
      swatchC.style.background = COLOURS.satRed;
    }
    note.style.display = state.gradient ? '' : 'none';
  }

  function wirePopupToggles() {
    // Delegated from the map container: popup content is rebuilt on every open,
    // so a listener bound to the button itself would not survive.
    document.getElementById('map').addEventListener('click', (ev) => {
      if (!ev.target.closest('.popup-tables-toggle')) return;
      state.popupTablesOpen = !state.popupTablesOpen;
      reopenSelectedPopup();
    });
  }

  // Never awaited by a handler: the map stays interactive for the whole
  // download, and a failure leaves the base map working. Refreshes the open
  // popup so a chart the user is already looking at fills itself in.
  function loadHistoryInBackground() {
    if (historyData?.[state.metric]) return;
    historyError = false;
    ensureHistoryLoaded()
      .then(() => {
        if (state.selectedSchool != null) reopenSelectedPopup();
      })
      .catch(e => {
        // Say so rather than leaving "Ładowanie…" in the popup forever.
        console.error('history load failed', e);
        historyError = true;
        if (state.selectedSchool != null) reopenSelectedPopup();
      });
  }

  // ---------------------------------------------------------------------------
  // Wiring UI controls

  function wireControls() {
    const subjectSel = document.getElementById('subject-select');
    const metricSel  = document.getElementById('metric-select');
    fillSubjectSelect(subjectSel, state.subject);
    fillMetricSelect(metricSel,   state.metric, state.advancedMetrics);

    subjectSel.addEventListener('change', e => onSubjectChange(e.target.value));
    metricSel .addEventListener('change', e => onMetricChange(e.target.value));

    // Public/private radios
    for (const r of document.querySelectorAll('input[name="public"]')) {
      r.removeAttribute('checked');
      if (r.value === state.publicFilter) { r.checked = true; r.setAttribute('checked', ''); }
      r.addEventListener('change', () => {
        state.publicFilter = r.value;
        syncURL();
        refreshFilters();
      });
    }

    // Threshold slider
    syncThresholdSlider();
    const slider = document.getElementById('threshold-slider');
    const display = document.getElementById('threshold-display');
    slider.addEventListener('input', () => {
      state.threshold = parseFloat(slider.value);
      const range = scaleData.metadata.slider_ranges[baselineLevel][state.metric];
      display.textContent = (state.threshold === range.min)
        ? '—' : fmtScore(state.threshold, state.metric);
      syncURL();
      refreshFilters();
    });

    // Min years slider. Max = number of dataset years, so it scales when a new
    // exam year lands; also clamp a too-large minYears carried over from an older URL.
    const myr = document.getElementById('min-years-slider');
    const myrDisp = document.getElementById('min-years-display');
    const maxYears = scaleData.metadata.years_in_data.length;
    myr.max = maxYears;
    state.minYears = Math.min(state.minYears, maxYears);
    myr.value = state.minYears;
    myrDisp.textContent = state.minYears;
    myr.addEventListener('input', () => {
      state.minYears = parseInt(myr.value, 10);
      myrDisp.textContent = state.minYears;
      syncURL();
      refreshFilters();
    });

    // Advanced-metrics toggle — shared preference, so ticking it here also
    // unlocks the metrics in the ranking.
    const advancedCB = document.getElementById('advanced-metrics-toggle');
    advancedCB.checked = state.advancedMetrics;
    advancedCB.addEventListener('change', () => {
      state.advancedMetrics = advancedCB.checked;
      writePref('advanced_metrics', state.advancedMetrics);
      // Turning it off while an advanced metric colours the map would strand the
      // user on a metric they can no longer reselect — fall back to the default.
      if (!state.advancedMetrics && isAdvancedMetric(state.metric)) {
        onMetricChange(DEFAULTS.metric);
      }
      fillMetricSelect(metricSel, state.metric, state.advancedMetrics);
    });

    // Gradient toggle (continuous colour in B/D bands)
    const gradCb = document.getElementById('gradient-toggle');
    gradCb.checked = state.gradient;
    if (state.gradient) gradCb.setAttribute('checked', '');
    gradCb.addEventListener('change', () => {
      state.gradient = gradCb.checked;
      writePref('gradient', state.gradient);
      syncURL();
      recolourAll();
      syncLegend();
    });
    syncLegend();

    wireSearch();
    wireSchoolFind();
    wirePopupToggles();
    wireNavLinks();

    // Language toggle: re-translate the dynamic, JS-built content that
    // applyI18N (static [data-i18n] only) can't reach — the metric/subject
    // selects, the filter-count summary, and any open popup.
    wireLangToggle(() => {
      state.lang = currentLang;
      syncURL();
      fillSubjectSelect(subjectSel, state.subject);
      fillMetricSelect(metricSel, state.metric, state.advancedMetrics);
      fillDataYears();
      refreshFilters();
      if (state.selectedSchool != null) reopenSelectedPopup();
    });
  }

  function wireNavLinks() {
    // Carry metric/subject/lang to the ranking page nav link.
    const link = document.querySelector('.topnav nav a[href="ranking.html"]');
    if (!link) return;
    const update = () => {
      const usp = new URLSearchParams();
      if (state.metric  !== DEFAULTS.metric)  usp.set('metric',  state.metric);
      if (state.subject !== DEFAULTS.subject) usp.set('subject', state.subject);
      if (state.lang    !== DEFAULTS.lang)    usp.set('lang',    state.lang);
      const qs = usp.toString();
      link.href = 'ranking.html' + (qs ? '?' + qs : '');
    };
    update();
    // Re-update on any nav-affecting state change. Simpler than wiring observers:
    // recompute on any pointerdown over the link.
    link.addEventListener('pointerdown', update);
  }

  function openInitialPopup() {
    if (state.selectedSchool == null) return;
    const marker = markersByRspo.get(state.selectedSchool);
    if (marker) {
      // Wait for cluster to settle before zooming.
      clusterGroup.zoomToShowLayer(marker, () => {
        marker.openPopup();
      });
    }
  }

  // ---------------------------------------------------------------------------
  // Bootstrap

  async function main() {
    resolveInitialState();
    setLang(state.lang);
    initMap();
    try {
      await Promise.all([loadIndex(), loadScale()]);
    } catch (e) {
      console.error(e);
      document.body.innerHTML = '<p style="padding:1rem">Nie udało się wczytać danych: ' + e.message + '</p>';
      return;
    }
    buildClusterGroup();
    buildSchoolSearchIndex();
    fillDataYears();
    wireControls();
    syncURL();          // canonicalise the URL (e.g. add resolved threshold)
    openInitialPopup(); // if ?school=… was in the URL
    loadHistoryInBackground();  // popups' year-by-year charts, no button to press

    map.on('zoomend moveend', () => { renderLevel(); });
    renderLevel();   // the opening view: the events above only fire on interaction
  }

  main();
})();
