// Ranking page: sortable/filterable table of schools by (metric, subject, view).
// Scores and the LOO/single_year/last_k views now live in the per-powiat shards
// (docs/data/powiat/{teryt4}-{metric}.json), so the table has no whole-country
// population to read; Task 16's level control decides what it ranks.

(function () {
  const state = {
    metric: DEFAULTS.metric,
    subject: DEFAULTS.subject,
    view: 'base',                 // 'base' | 'last_k' | 'single_year' | 'loo'
    viewParam: null,              // string: year ('2023') or k ('3')
    publicFilter: 'all',
    nameQuery: '',
    sortKey: 'rank',
    sortDir: 'asc',
    selectedSchool: null,
    lang: DEFAULTS.lang,
    advancedMetrics: false,
    // Detail-panel UI (ephemeral, not in the URL):
    detailTablesOpen: false,        // numeric tables hidden until asked for
    detailTableDim: 'score',        // 'score' | 'rank' | 'pct'
  };

  const ALLOWED_VIEWS = ['base', 'last_k', 'single_year', 'loo'];

  // The schools the table ranks. Empty until Task 16 gives this page its level
  // control, which decides what populates it: regions rank nationally from
  // regions-{level}.json, schools only within a chosen region's shards.
  let loadedSchools = [];

  // History data fetched in the background (per-metric).
  const histByMetric = {};
  let historyError = false;       // last background fetch failed

  // ---------------------------------------------------------------------------
  // State resolution

  function resolveInitialState() {
    state.advancedMetrics = resolveAdvancedMetrics();
    state.metric  = resolvePref('metric',  METRICS);
    state.subject = resolvePref('subject', SUBJECTS);
    state.lang    = resolvePref('lang',    ['pl', 'en']);

    const url = getURLParams();
    const view = url.get('view');
    if (view && ALLOWED_VIEWS.includes(view)) state.view = view;
    state.viewParam = url.get('view_param') || null;

    const pub = url.get('public');
    if (pub === 'tak' || pub === 'nie' || pub === 'all') state.publicFilter = pub;

    state.nameQuery = url.get('q') || '';

    const sort = url.get('sort');
    if (sort) state.sortKey = sort;
    const dir = url.get('dir');
    if (dir === 'asc' || dir === 'desc') state.sortDir = dir;

    const school = parseInt(url.get('school'), 10);
    state.selectedSchool = Number.isInteger(school) ? school : null;
  }

  function syncURL() {
    setURLParams({
      metric:     state.metric  !== DEFAULTS.metric  ? state.metric  : null,
      subject:    state.subject !== DEFAULTS.subject ? state.subject : null,
      view:       state.view !== 'base' ? state.view : null,
      view_param: state.view !== 'base' ? state.viewParam : null,
      public:     state.publicFilter !== 'all' ? state.publicFilter : null,
      q:          state.nameQuery || null,
      sort:       state.sortKey !== 'rank' ? state.sortKey : null,
      dir:        state.sortDir !== 'asc' ? state.sortDir : null,
      school:     state.selectedSchool,
      lang:       state.lang !== DEFAULTS.lang ? state.lang : null,
    });
  }

  // ---------------------------------------------------------------------------
  // Row construction

  // For each school, produce a flat object with the fields we sort/render.
  function buildRow(school) {
    const { metric, subject } = state;
    const base = school.scores?.[metric]?.[subject];

    // For non-base views, look up the per-metric/per-subject data if loaded.
    let viewScore = null, viewRank = null;
    let looMinR = null, looMaxR = null;
    let singleMinR = null, singleMaxR = null;

    const hist = histByMetric[metric]?.schools?.[String(school.rspo)]?.[subject];
    if (hist) {
      // LOO range
      const loo = hist.loo || {};
      const looRanks = Object.values(loo).map(v => v?.rank).filter(v => v != null);
      if (looRanks.length) {
        looMinR = Math.min(...looRanks);
        looMaxR = Math.max(...looRanks);
      }
      // single_year range
      const sy = hist.single_year || {};
      const syRanks = Object.values(sy).map(v => v?.rank).filter(v => v != null);
      if (syRanks.length) {
        singleMinR = Math.min(...syRanks);
        singleMaxR = Math.max(...syRanks);
      }

      // Selected view
      if (state.view === 'base') {
        viewScore = hist.base?.score ?? null;
        viewRank  = hist.base?.rank  ?? null;
      } else if (state.viewParam) {
        const cell = hist[state.view]?.[state.viewParam];
        viewScore = cell?.score ?? null;
        viewRank  = cell?.rank  ?? null;
      }
    }

    // For base view without history loaded, fall back to the row's own base scores.
    if (state.view === 'base') {
      viewScore = base?.score ?? null;
      viewRank  = base?.rank  ?? null;
    }

    // A/B/C class for the displayed score, bucketed against the base
    // distribution (same sigma/centre the map uses), so map and ranking agree.
    // The cell is coloured by the continuous gradient (not a flat class colour),
    // so two schools either side of a boundary look almost the same — the letter
    // flips but the colour barely moves, showing the boundary is soft.
    const { sigma, sigma_centre: centre } = scaleFor(metric, subject);
    const { p1, p99 } = scoreExtent(metric, subject);
    const classIndex = classIndex3(viewScore, centre, sigma);
    const classColour = classIndex == null ? null : gradient3Colour(viewScore, centre, sigma, p1, p99);

    return {
      rspo: school.rspo,
      school,
      name: school.name,
      street: school.ulica_nr,
      town: school.miejscowosc,
      gmina: school.gmina,
      powiat: school.powiat,
      pub: isPublic(school),
      n_years: school.n_years,
      hasCoords: school.lat != null && school.lon != null,
      score: viewScore,
      rank: viewRank,
      classIndex,
      classLetter: classIndex == null ? null : CLASS3_LETTERS[classIndex],
      classColour,
      classTextColour: classColour ? textOn(classColour) : null,
      looMinR, looMaxR,
      singleMinR, singleMaxR,
    };
  }

  // ---------------------------------------------------------------------------
  // Filtering + sorting

  function filterRows(rows) {
    const q = state.nameQuery.trim().toLowerCase();
    return rows.filter(r => {
      if (state.publicFilter === 'tak' && !r.pub) return false;
      if (state.publicFilter === 'nie' &&  r.pub) return false;
      if (q) {
        const inName = r.name.toLowerCase().includes(q);
        const inTown = (r.town || '').toLowerCase().includes(q);
        const inStreet = (r.street || '').toLowerCase().includes(q);
        const inGmina = (r.gmina || '').toLowerCase().includes(q);
        const inPowiat = (r.powiat || '').toLowerCase().includes(q);
        if (!inName && !inTown && !inStreet && !inGmina && !inPowiat) return false;
      }
      // Drop rows where the view's score is missing — they can't be ranked here.
      if (r.score == null) return false;
      return true;
    });
  }

  function sortRows(rows) {
    const key = state.sortKey;
    const dir = state.sortDir === 'asc' ? 1 : -1;
    rows.sort((a, b) => {
      const va = a[key], vb = b[key];
      if (va == null && vb == null) return 0;
      if (va == null) return 1;          // nulls last regardless of dir
      if (vb == null) return -1;
      if (typeof va === 'string') return va.localeCompare(vb, 'pl') * dir;
      return (va - vb) * dir;
    });
    return rows;
  }

  // ---------------------------------------------------------------------------
  // Rendering

  const COLUMNS = [
    { key: 'rank',       i18n: 'colRank',        num: true,  width: '4rem' },
    { key: 'name',       i18n: 'colName',        num: false },
    { key: 'town',       i18n: 'colTown',        num: false },
    { key: 'street',     i18n: 'colStreet',      num: false },
    { key: 'pub',        i18n: 'colPublic',      num: false, width: '5rem' },
    { key: 'n_years',    i18n: 'colNYears',      num: true,  width: '4rem' },
    { key: 'score',       i18n: 'colScore',       num: true },
    { key: 'classLetter', i18n: 'colClass',       num: true,  width: '4rem' },
    { key: 'looMinR',     i18n: 'colLOORange',    num: true,  help: 'helpLOORange' },
    { key: 'singleMinR',  i18n: 'colSingleRange', num: true,  help: 'helpSingleRange' },
    { key: 'gmina',       i18n: 'colGmina',       num: false },
    { key: 'powiat',      i18n: 'colPowiat',      num: false },
  ];

  // A/B/C badge coloured by the continuous gradient (soft boundaries).
  function classBadge(score, centre, sigma, p1, p99) {
    const letter = classLetter3(score, centre, sigma);
    if (letter == null) return '<span class="class-badge class-badge-sm">—</span>';
    const bg = gradient3Colour(score, centre, sigma, p1, p99);
    return `<span class="class-badge class-badge-sm" style="background:${bg};color:${textOn(bg)}">${letter}</span>`;
  }

  const DETAIL_SUBJECTS = ['polski', 'matematyka', 'angielski', 'composite_min'];

  // Short formatter per dimension shown on a chart axis / in a table cell.
  function dimFmt(dim) {
    if (dim === 'rank') return (v) => '#' + Math.round(v);
    // No "%" on a percentile: it is a position in the distribution, not a share
    // of anything the school scored. The neighbouring score column is a real
    // percentage (of points), so marking both invites reading them alike.
    if (dim === 'pct')  return (v) => v.toFixed(1);
    return (v) => fmtScore(v, state.metric);
  }

  // One chart for a (view, dimension): a line per subject over the years.
  function detailChart(hist, years, view, dim) {
    const series = DETAIL_SUBJECTS.map(subj => ({
      colour: SUBJECT_COLOURS[subj],
      // Dots only for composite_min so it doesn't hide the subject lines.
      pointsOnly: subj === 'composite_min',
      points: Object.fromEntries(years.map(y => {
        const cell = hist[subj]?.[view]?.[String(y)];
        return [y, cell ? cell[dim] : null];
      })),
    }));
    return lineChartSVG({
      years, series,
      invertY: dim === 'rank',          // rank 1 = best, at the top
      fmtY: dimFmt(dim),
      width: 210, height: 124,
    });
  }

  // A numeric table for one view (single_year / loo / last_k) and the current
  // table dimension. Bolds the weakest of the 3 core subjects per row (the one
  // that drives composite_min — by score, regardless of the dimension shown).
  function detailTable(hist, keys, view, dim) {
    if (!keys.length) return '';
    const fmt = dimFmt(dim);
    const core = ['polski', 'matematyka', 'angielski'];
    const bodyRows = keys.map(key => {
      let weakest = null, weakestScore = Infinity;
      for (const s of core) {
        const sc = hist[s]?.[view]?.[String(key)]?.score;
        if (sc != null && sc < weakestScore) { weakestScore = sc; weakest = s; }
      }
      const cells = DETAIL_SUBJECTS.map(s => {
        const cell = hist[s]?.[view]?.[String(key)];
        const val = (cell && cell[dim] != null) ? fmt(cell[dim]) : '—';
        return `<td class="num${s === weakest ? ' weakest' : ''}">${val}</td>`;
      }).join('');
      const label = (view === 'last_k') ? t('lastKRow', key) : key;
      return `<tr><th>${label}</th>${cells}</tr>`;
    }).join('');
    const header = `<tr><th></th>${DETAIL_SUBJECTS.map(s => `<th>${t('subject_' + s)}</th>`).join('')}</tr>`;
    const title = view === 'single_year' ? t('detailSecSingle')
                : view === 'loo' ? t('detailSecLOO') : t('detailSecLastK');
    // <caption> is part of the table, so the title always stays directly above
    // its table and wraps together with it (a separate sibling div could drift
    // onto a different line on narrow screens).
    return `<table class="detail-table">
        <caption>${title}</caption>
        <thead>${header}</thead><tbody>${bodyRows}</tbody>
      </table>`;
  }

  // The "all years" (base) aggregate per subject — always present, so even schools
  // with < 3 years (no LOO / last_k folds) still get a meaningful numeric table.
  // Mirrors the map popup, which shows the base value for every subject.
  function detailBaseTable(hist, dim) {
    const fmt = dimFmt(dim);
    const core = ['polski', 'matematyka', 'angielski'];
    let weakest = null, weakestScore = Infinity;
    for (const s of core) {
      const sc = hist[s]?.base?.score;
      if (sc != null && sc < weakestScore) { weakestScore = sc; weakest = s; }
    }
    const cells = DETAIL_SUBJECTS.map(s => {
      const cell = hist[s]?.base;
      const val = (cell && cell[dim] != null) ? fmt(cell[dim]) : '—';
      return `<td class="num${s === weakest ? ' weakest' : ''}">${val}</td>`;
    }).join('');
    const header = `<tr><th></th>${DETAIL_SUBJECTS.map(s => `<th>${t('subject_' + s)}</th>`).join('')}</tr>`;
    return `<table class="detail-table">
        <caption>${t('detailSecBase')}</caption>
        <thead>${header}</thead><tbody><tr><th></th>${cells}</tr></tbody>
      </table>`;
  }

  // The expandable detail panel: class trajectory for the selected subject, a
  // 2×3 grid of charts (LOO / single-year × score / rank / percentile) showing
  // all subjects, and numeric tables behind a toggle. Needs the per-metric
  // history file, which is fetched in the background from page load — so the
  // only time it is absent is while that fetch is still in flight.
  function renderDetailRow(row) {
    const colspan = COLUMNS.length;
    const hist = histByMetric[state.metric]?.schools?.[String(row.rspo)];
    if (!hist) {
      return `<tr class="detail-row"><td colspan="${colspan}">
        <span class="muted small">${t(historyError ? 'historyFailed' : 'historyLoading')}</span>
      </td></tr>`;
    }

    const years = scaleData.metadata.years_in_data;
    const yearsPresent = years.filter(y =>
      DETAIL_SUBJECTS.some(s => hist[s]?.single_year?.[String(y)] != null));

    // Class trajectory for the SELECTED subject (composite_min when that's the
    // chosen subject), plus how often the school lands in each class.
    const subj = state.subject;
    const { sigma: cSigma, sigma_centre: cCentre } = scaleFor(state.metric, subj);
    const { p1: cP1, p99: cP99 } = scoreExtent(state.metric, subj);
    const counts = [0, 0, 0];
    const trajBadges = yearsPresent.map(y => {
      const sc = hist[subj]?.single_year?.[String(y)]?.score;
      const ci = classIndex3(sc, cCentre, cSigma);
      if (ci != null) counts[ci]++;
      return `<span class="traj-item">${y}&nbsp;${classBadge(sc, cCentre, cSigma, cP1, cP99)}</span>`;
    }).join(' ');
    const countSummary = [2, 1, 0]
      .filter(i => counts[i] > 0).map(i => `${CLASS3_LETTERS[i]}×${counts[i]}`).join('  ');

    // 2×3 chart grid: rows = LOO / single-year, cols = score / rank / percentile.
    const dims = [
      { dim: 'score', label: t('popupScore') },
      { dim: 'rank',  label: t('popupRank') },
      { dim: 'pct',   label: t('popupPct'), help: 'helpPct' },
    ];
    // The grid's two rows are the same numbers computed two different ways, and
    // the row labels alone ("LOO", "pojedyncze lata") don't say what separates
    // them. One disclosure above the grid explains both, since the point is the
    // contrast between them.
    const chartGrid = `
      ${helpDetailsHTML('chartDiffCaption', 'helpChartDiff')}
      <div class="chart-grid">
        <div class="cg-corner"></div>
        ${dims.map(d => `<div class="cg-colhead">${d.label}${d.help ? helpIconHTML(d.help) : ''}</div>`).join('')}
        <div class="cg-rowhead">${t('detailViewLOO')}</div>
        ${dims.map(d => `<div class="cg-cell">${detailChart(hist, yearsPresent, 'loo', d.dim)}</div>`).join('')}
        <div class="cg-rowhead">${t('detailViewSingle')}</div>
        ${dims.map(d => `<div class="cg-cell">${detailChart(hist, yearsPresent, 'single_year', d.dim)}</div>`).join('')}
      </div>
      <div class="chart-legend">${subjectLegendHTML(DETAIL_SUBJECTS)}</div>`;

    // Numeric tables, behind a toggle (lots of data; charts usually suffice).
    let tablesBlock;
    if (!state.detailTablesOpen) {
      tablesBlock = `<button type="button" class="detail-tables-toggle">${t('detailShowTables')}</button>`;
    } else {
      const ks = Object.keys(hist[DETAIL_SUBJECTS[0]]?.last_k || {}).sort((a, b) => +a - +b);
      const dimSwitch = dims.map(d =>
        `<button type="button" class="detail-dim-btn${state.detailTableDim === d.dim ? ' active' : ''}" data-dim="${d.dim}">${d.label}</button>`
      ).join('');
      tablesBlock = `
        <button type="button" class="detail-tables-toggle">${t('detailHideTables')}</button>
        <div class="detail-dim-switch">${dimSwitch}</div>
        <p class="muted small detail-tables-note">${t('detailWeakestNote')}</p>
        <div class="detail-tables">
          ${detailTable(hist, yearsPresent, 'single_year', state.detailTableDim)}
          ${detailTable(hist, yearsPresent, 'loo', state.detailTableDim)}
          ${detailTable(hist, ks, 'last_k', state.detailTableDim)}
          ${detailBaseTable(hist, state.detailTableDim)}
        </div>`;
    }

    return `<tr class="detail-row"><td colspan="${colspan}">
      <div class="detail-panel">
        <div class="detail-traj">
          <strong>${t('detailClassByYear', t('subject_' + subj))}:</strong> ${trajBadges}
          <span class="detail-counts">(${countSummary})</span>
        </div>
        ${chartGrid}
        ${tablesBlock}
      </div>
    </td></tr>`;
  }

  function renderTable(rows) {
    const table = document.getElementById('ranking-table');
    const head = `<thead><tr>${COLUMNS.map(col => {
      const indicator = (state.sortKey === col.key)
        ? `<span class="sort-indicator">${state.sortDir === 'asc' ? '▲' : '▼'}</span>` : '';
      const style = col.width ? ` style="width:${col.width};"` : '';
      const help = col.help
        ? ` <span class="help-icon" tabindex="0" role="button" aria-label="?" data-help="${escapeHTML(t(col.help))}">i</span>`
        : '';
      return `<th data-col="${col.key}" class="${col.num ? 'num' : ''}"${style}>${t(col.i18n)}${help}${indicator}</th>`;
    }).join('')}</tr></thead>`;

    const body = `<tbody>${rows.map(r => {
      const offMap = !r.hasCoords ? ` <span class="off-map" title="${t('offMap')}">📍✗</span>` : '';
      const looCell = (r.looMinR != null) ? `${r.looMinR}–${r.looMaxR}` : '—';
      const syCell  = (r.singleMinR != null) ? `${r.singleMinR}–${r.singleMaxR}` : '—';
      const pubLabel = r.pub ? t('publicYesShort') : t('publicNoShort');
      const classCell = (r.classLetter)
        ? `<span class="class-badge" style="background:${r.classColour};color:${r.classTextColour}">${r.classLetter}</span>`
        : '—';
      const selected = (r.rspo === state.selectedSchool);
      const mainRow = `<tr data-rspo="${r.rspo}"${selected ? ' class="highlight"' : ''}>
        <td class="num">${r.rank ?? '—'}</td>
        <td>${escapeHTML(r.name)}${offMap}</td>
        <td>${escapeHTML(r.town || '')}</td>
        <td>${escapeHTML(r.street || '')}</td>
        <td>${pubLabel}</td>
        <td class="num">${r.n_years}${r.n_years < 3 ? ` <span class="warn-icon" title="${escapeHTML(t('warnShortHistory'))}">⚠️</span>` : ''}</td>
        <td class="num">${fmtScoreHTML(r.score, state.metric)}</td>
        <td class="num class-cell">${classCell}</td>
        <td class="num">${looCell}</td>
        <td class="num">${syCell}</td>
        <td>${escapeHTML(r.gmina || '')}</td>
        <td>${escapeHTML(r.powiat || '')}</td>
      </tr>`;
      // A selected row expands an inline detail panel below it.
      return mainRow + (selected ? renderDetailRow(r) : '');
    }).join('')}</tbody>`;

    table.innerHTML = head + body;

    // Wire header sort. Ignore clicks on the help icon (it shows a tooltip on
    // hover/focus; tapping it must not also re-sort the column).
    for (const th of table.querySelectorAll('thead th')) {
      th.addEventListener('click', (e) => {
        if (e.target.closest('.help-icon')) return;
        onSortClick(th.getAttribute('data-col'));
      });
    }
    // Wire main-row click → toggle selected (expands detail + deep link). Scoped
    // to [data-rspo] so clicks inside the detail row don't collapse it.
    for (const tr of table.querySelectorAll('tbody tr[data-rspo]')) {
      tr.addEventListener('click', () => {
        const rspo = parseInt(tr.getAttribute('data-rspo'), 10);
        state.selectedSchool = (state.selectedSchool === rspo) ? null : rspo;
        state.detailTablesOpen = false;   // each newly opened school starts collapsed
        syncURL();
        renderAll();
      });
    }
    // Detail panel: toggle the numeric tables, and switch their dimension.
    const tablesToggle = table.querySelector('.detail-tables-toggle');
    if (tablesToggle) {
      tablesToggle.addEventListener('click', (e) => {
        e.stopPropagation();
        state.detailTablesOpen = !state.detailTablesOpen;
        renderAll();
      });
    }
    for (const db of table.querySelectorAll('.detail-dim-btn')) {
      db.addEventListener('click', (e) => {
        e.stopPropagation();
        state.detailTableDim = db.getAttribute('data-dim');
        renderAll();
      });
    }
  }

  function escapeHTML(s) {
    if (s == null) return '';
    return String(s)
      .replaceAll('&', '&amp;')
      .replaceAll('<', '&lt;')
      .replaceAll('>', '&gt;')
      .replaceAll('"', '&quot;');
  }

  function onSortClick(key) {
    if (state.sortKey === key) {
      state.sortDir = state.sortDir === 'asc' ? 'desc' : 'asc';
    } else {
      state.sortKey = key;
      state.sortDir = (key === 'name' || key === 'town') ? 'asc' : 'asc';
    }
    syncURL();
    renderAll();
  }

  function renderAll() {
    const rows = loadedSchools.map(buildRow);
    const filtered = sortRows(filterRows(rows));
    document.getElementById('ranking-info').textContent =
      t('rowsShown', filtered.length, loadedSchools.length);
    renderTable(filtered);
    if (state.selectedSchool != null) {
      const tr = document.querySelector(`tr[data-rspo="${state.selectedSchool}"]`);
      if (tr) tr.scrollIntoView({ block: 'center', behavior: 'auto' });
    }
  }

  // ---------------------------------------------------------------------------
  // View / view_param management

  function updateViewParamField() {
    const wrap = document.getElementById('view-param-field');
    const sel = document.getElementById('view-param-select');
    sel.innerHTML = '';
    const years = scaleData.metadata.years_in_data;
    let options = [];
    if (state.view === 'single_year' || state.view === 'loo') {
      options = years.map(y => ({ value: String(y), label: String(y) }));
    } else if (state.view === 'last_k') {
      // k = 2..max(years)-1; safest to expose 2..(n_years-1) per dataset
      options = [];
      for (let k = 2; k < years.length; k++) options.push({ value: String(k), label: String(k) });
    }
    if (options.length === 0) {
      wrap.style.display = 'none';
      state.viewParam = null;
      return;
    }
    for (const o of options) {
      const opt = document.createElement('option');
      opt.value = o.value;
      opt.textContent = o.label;
      sel.appendChild(opt);
    }
    // Pick a sensible default if the current viewParam doesn't apply.
    if (!options.find(o => o.value === state.viewParam)) {
      state.viewParam = options[options.length - 1].value;  // latest year / largest k
    }
    sel.value = state.viewParam;
    wrap.style.display = '';
  }

  // ---------------------------------------------------------------------------
  // Per-metric history file
  //
  // The rank-range columns, the non-base views and the detail panel all need it,
  // so it is fetched for whatever metric is selected — always, in the background,
  // never behind a checkbox. Asking first made the ranking look broken: picking
  // a non-base view showed "—" everywhere until you spotted an opt-in row well
  // above the table and guessed it was related.

  async function ensureMetricLoaded() {
    // No-op until Task 16, which owns this page: the per-metric whole-country
    // files are gone, and their replacement, loadShard, is per-powiat — so it
    // needs the region that Task 16's level control selects. Not an oversight.
  }

  // Fetch in the background and re-render when it lands. Never awaited by a
  // handler, so the table stays interactive for the whole download; a failure
  // leaves the base view working rather than blanking the page.
  function loadMetricInBackground() {
    if (histByMetric[state.metric]) return;
    historyError = false;
    ensureMetricLoaded()
      .then(renderAll)
      .catch(e => {
        // Say so rather than leaving "Ładowanie…" on screen forever.
        console.error('history load failed', e);
        historyError = true;
        renderAll();
      });
  }

  // ---------------------------------------------------------------------------
  // Controls wiring

  function wireControls() {
    const metricSel  = document.getElementById('metric-select');
    const subjectSel = document.getElementById('subject-select');
    const viewSel    = document.getElementById('view-select');
    const viewParamSel = document.getElementById('view-param-select');
    const nameInput  = document.getElementById('name-search');

    fillMetricSelect(metricSel,   state.metric, state.advancedMetrics);
    fillSubjectSelect(subjectSel, state.subject);
    viewSel.value = state.view;

    const advancedCB = document.getElementById('advanced-metrics-toggle');
    advancedCB.checked = state.advancedMetrics;
    advancedCB.addEventListener('change', () => {
      state.advancedMetrics = advancedCB.checked;
      writePref('advanced_metrics', state.advancedMetrics);
      // Turning it off while an advanced metric is selected would leave the
      // table showing a metric with no way back to it — fall back to the default.
      if (!state.advancedMetrics && isAdvancedMetric(state.metric)) {
        state.metric = DEFAULTS.metric;
        writePref('metric', state.metric);
        loadMetricInBackground();
      }
      fillMetricSelect(metricSel, state.metric, state.advancedMetrics);
      syncURL();
      renderAll();
    });

    metricSel.addEventListener('change', () => {
      state.metric = metricSel.value;
      writePref('metric', state.metric);
      syncURL();
      renderAll();
      // The range columns need the new metric's file even on the base view.
      loadMetricInBackground();
    });

    subjectSel.addEventListener('change', () => {
      state.subject = subjectSel.value;
      writePref('subject', state.subject);
      syncURL();
      renderAll();
    });

    viewSel.addEventListener('change', () => {
      state.view = viewSel.value;
      updateViewParamField();
      syncURL();
      renderAll();
      loadMetricInBackground();
    });

    viewParamSel.addEventListener('change', () => {
      state.viewParam = viewParamSel.value;
      syncURL();
      renderAll();
    });

    for (const r of document.querySelectorAll('input[name="public"]')) {
      r.removeAttribute('checked');
      if (r.value === state.publicFilter) { r.checked = true; r.setAttribute('checked', ''); }
      r.addEventListener('change', () => {
        state.publicFilter = r.value;
        syncURL();
        renderAll();
      });
    }

    nameInput.value = state.nameQuery;
    nameInput.addEventListener('input', () => {
      state.nameQuery = nameInput.value;
      syncURL();
      renderAll();
    });

    wireNavLinks();

    // Language toggle: re-translate JS-built content (metric/subject selects
    // and the whole table, whose headers/labels come from t()). The view-select
    // options carry data-i18n, so setLang's applyI18N already handles them.
    wireLangToggle(() => {
      state.lang = currentLang;
      syncURL();
      fillMetricSelect(metricSel, state.metric, state.advancedMetrics);
      fillSubjectSelect(subjectSel, state.subject);
      fillDataYears();
      renderAll();
    });
  }

  function wireNavLinks() {
    const link = document.querySelector('.topnav nav a[href="index.html"]');
    if (!link) return;
    const update = () => {
      const usp = new URLSearchParams();
      if (state.metric  !== DEFAULTS.metric)  usp.set('metric',  state.metric);
      if (state.subject !== DEFAULTS.subject) usp.set('subject', state.subject);
      if (state.lang    !== DEFAULTS.lang)    usp.set('lang',    state.lang);
      if (state.selectedSchool != null)       usp.set('school',  state.selectedSchool);
      const qs = usp.toString();
      link.href = 'index.html' + (qs ? '?' + qs : '');
    };
    update();
    link.addEventListener('pointerdown', update);
  }

  // ---------------------------------------------------------------------------
  // Bootstrap

  async function main() {
    resolveInitialState();
    setLang(state.lang);
    try {
      await Promise.all([loadIndex(), loadScale()]);
    } catch (e) {
      console.error(e);
      document.body.innerHTML = '<p style="padding:1rem">Nie udało się wczytać danych: ' + e.message + '</p>';
      return;
    }
    wireControls();
    fillDataYears();
    updateViewParamField();

    // Render immediately from whatever population is loaded. Empty until Task 16.
    renderAll();
    syncURL();

    // Then fill the range columns, the non-base views and the detail panels from
    // the shards. Unawaited, so the page stays usable for the whole download.
    loadMetricInBackground();
  }

  main();
})();
