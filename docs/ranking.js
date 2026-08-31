// Ranking page: a sortable/filterable table of whatever the level control picks
// — voivodeships, powiats, gminas or schools — by (metric, subject, view).
//
// The four levels are deliberately NOT symmetric:
//   voivodeship / powiat / gmina  rank NATIONALLY, straight from
//     regions-{level}.json. Those files are 2.8 / 49 / 284 KB gzipped and
//     already whole-country, so the twenty best gminas in Poland cost nothing.
//   school                        ranks WITHIN ONE POWIAT, from that powiat's
//     shard (docs/data/powiat/{teryt4}-{metric}.json). A national school ranking
//     would need every school's scores at every reference level in one file,
//     ~5 MB gzipped — exactly the payload the per-powiat sharding exists to
//     avoid. So with no powiat chosen the page shows a prompt rather than
//     quietly ranking one powiat's worth and calling it a ranking.

(function () {
  const RANK_LEVELS = ['voivodeship', 'powiat', 'gmina', 'school'];
  const REGION_LEVELS = ['voivodeship', 'powiat', 'gmina'];
  // Whose name the "parent" column shows. Voivodeships have no parent (their
  // `parent` field is ''), so they get no such column.
  const PARENT_LEVEL = { powiat: 'voivodeship', gmina: 'powiat' };
  // `pct` is scoped to the siblings under the same parent, so the header names
  // that scope — the rank beside it is national and the two must not be read
  // as the same denominator.
  const PCT_COLUMN = {
    voivodeship: 'colPctInCountry',      // all 16 share the empty parent
    powiat: 'colPctInVoivodeship',
    gmina: 'colPctInPowiat',
  };
  const ROWS_SHOWN = {
    voivodeship: 'rowsShownVoivodeship',
    powiat: 'rowsShownPowiat',
    gmina: 'rowsShownGmina',
    school: 'rowsShown',
  };

  const state = {
    level: 'school',              // one of RANK_LEVELS
    region: null,                 // 4-digit powiat TERYT; school level only
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

  // What the table currently ranks. Exactly one of the two is populated:
  // regionData at a region level, schoolRows at school level.
  let regionData = null;    // the regions-{level}.json payload for state.level
  let schoolRows = [];      // school objects built from state.region's shard
  // The same shard, indexed for the rank-range columns, the non-base views and
  // the detail panel — it carries base/loo/single_year/last_k already, so those
  // need no second fetch. { schools: { rspo: { subject: {views} } } }.
  let shardHistory = null;
  let populationError = false;
  // What is actually in memory, as a populationKey(). Guards two things at once:
  // a response that lost the race (regions-gmina.json is 1.59 MB and can land
  // after a 13 KB voivodeship file requested later), and rendering the previous
  // level's rows under the new level's column headers while the switch is in
  // flight. Anything that is not the current key counts as "not loaded".
  let loadedKey = null;

  function populationKey() {
    return state.level === 'school' ? `school|${state.region}|${state.metric}` : state.level;
  }

  function populationLoaded() { return loadedKey === populationKey(); }

  // ---------------------------------------------------------------------------
  // State resolution

  // Level and region are resolved here rather than through resolvePref: both are
  // this page's alone, so they must not join the DEFAULTS object that map.js
  // also reads, and their storage keys are namespaced for the same reason.
  function resolveLevel() {
    const url = getURLParams().get('level');
    if (RANK_LEVELS.includes(url)) return url;
    const stored = readPrefs().rank_level;
    if (RANK_LEVELS.includes(stored)) return stored;
    // The page is the school ranking; a default of "voivodeship" would rank
    // something its own title does not promise.
    return 'school';
  }

  function resolveRegion() {
    const url = getURLParams().get('region');
    if (/^\d{4}$/.test(url || '')) return url;
    const stored = readPrefs().rank_region;
    return /^\d{4}$/.test(stored || '') ? stored : null;
  }

  function resolveInitialState() {
    state.advancedMetrics = resolveAdvancedMetrics();
    state.metric  = resolvePref('metric',  METRICS);
    state.subject = resolvePref('subject', SUBJECTS);
    state.lang    = resolvePref('lang',    ['pl', 'en']);
    state.level   = resolveLevel();
    state.region  = resolveRegion();

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
      level:      state.level !== 'school' ? state.level : null,
      region:     state.level === 'school' ? state.region : null,
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

  // How many regions share each parent, per level — the second arm of the
  // suppression gate (a region that is its parent's ONLY CHILD is compared
  // against itself). Derived from regions.parent, which ships in the file.
  //
  // A near-twin of map.js's siblingCountsFor. Not shared through app.js because
  // map.js is not loaded on this page and unifying them would mean editing a
  // third file for no behavioural gain; escapeHTML is already duplicated across
  // the two page scripts for the same reason.
  const siblingCache = new Map();   // level -> Map(parent teryt -> child count)

  function siblingCountsFor(regions) {
    const level = regions.metadata.level;
    if (!siblingCache.has(level)) {
      const counts = new Map();
      for (const parent of regions.regions.parent) counts.set(parent, (counts.get(parent) || 0) + 1);
      siblingCache.set(level, counts);
    }
    return siblingCache.get(level);
  }

  // For one region, a flat object with the fields we sort/render. Unlike the
  // map, this page DOES render suppressed regions — a table enumerating a
  // population must not silently omit 69 of its 2,479 gminas — so a null score
  // yields a row carrying the reason instead of being dropped.
  function buildRegionRow(i) {
    const { metric, subject, level } = state;
    const r = regionData.regions;
    const score = r.score[metric][subject][i];

    // Flat class colours, not the gradient the school rows use: the region files
    // carry no p1/p99 anchors, and map.js colours its choropleth the same way —
    // so the same gmina reads as the same class in both places. Sigma/centre come
    // from THIS level's own metadata; regions and schools have different spreads.
    const { sigma, sigma_centre } = regionData.metadata;
    const classIndex = classIndex3(score, sigma_centre[metric][subject], sigma[metric][subject]);
    const classColour = classIndex == null ? null : CLASS3_FLAT[classIndex];

    const parentLevel = PARENT_LEVEL[level];
    return {
      key: r.teryt[i],
      name: r.name[i],
      parent: parentLevel ? nameOf(parentLevel, r.parent[i]) : null,
      n_schools: r.n_schools[i],
      n_students: r.n_students[i],
      score,
      rank: r.rank[metric][subject][i],
      pct: r.pct[metric][subject][i],
      classIndex,
      classLetter: classIndex == null ? null : CLASS3_LETTERS[classIndex],
      classColour,
      classTextColour: classColour ? textOn(classColour) : null,
      suppressed: score == null,
      // Say WHICH reason. A single "—" with no explanation is what makes a table
      // feel broken, and a wrong explanation is worse than none.
      reasonKey: score != null ? null
        : r.n_schools[i] === 0 ? 'regionNoSchools'
        : siblingCountsFor(regionData).get(r.parent[i]) <= 1 ? 'regionOnlyChild'
        : 'regionTooSmall',
    };
  }

  // For each school, produce a flat object with the fields we sort/render.
  function buildSchoolRow(school) {
    const { metric, subject } = state;
    const base = school.scores?.[metric]?.[subject];

    // For non-base views, look up the per-subject views from the loaded shard.
    let viewScore = null, viewRank = null;
    let looMinR = null, looMaxR = null;
    let singleMinR = null, singleMaxR = null;

    const hist = shardHistory?.schools?.[String(school.rspo)]?.[subject];
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
      suppressed: false,        // school rows with no score are filtered out
    };
  }

  // ---------------------------------------------------------------------------
  // Filtering + sorting

  function filterRows(rows) {
    const q = state.nameQuery.trim().toLowerCase();
    const schools = state.level === 'school';
    return rows.filter(r => {
      if (schools) {
        if (state.publicFilter === 'tak' && !r.pub) return false;
        if (state.publicFilter === 'nie' &&  r.pub) return false;
        // Drop rows where the view's score is missing — they can't be ranked
        // here. Region rows are kept instead and carry their reason: the table
        // IS the enumeration of that level's population, so an omitted region
        // would read as a region that does not exist.
        if (r.score == null) return false;
      }
      if (q) {
        const hay = schools
          ? [r.name, r.town, r.street, r.gmina, r.powiat]
          : [r.name, r.parent];
        if (!hay.some(v => (v || '').toLowerCase().includes(q))) return false;
      }
      return true;
    });
  }

  function sortRows(rows) {
    const key = state.sortKey;
    const dir = state.sortDir === 'asc' ? 1 : -1;
    rows.sort((a, b) => {
      // Suppressed regions sort last under every key and direction. They have a
      // name and a school count, so sorting by those would otherwise scatter
      // scoreless rows through a table whose point is the score.
      if (a.suppressed !== b.suppressed) return a.suppressed ? 1 : -1;
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

  // `label` is resolved at render time (renderAll re-runs on a language switch).
  // `helpArgs` are passed through to t(), so a help string can name the
  // denominator the column actually uses.
  function schoolColumns() {
    return [
      { key: 'rank',       label: t('colRankNational'), num: true,  width: '6rem',
        help: 'helpRankNational', helpArgs: [null] },
      { key: 'name',       label: t('colName'),        num: false },
      { key: 'town',       label: t('colTown'),        num: false },
      { key: 'street',     label: t('colStreet'),      num: false },
      { key: 'pub',        label: t('colPublic'),      num: false, width: '5rem' },
      { key: 'n_years',    label: t('colNYears'),      num: true,  width: '4rem' },
      { key: 'score',       label: t('colScore'),       num: true },
      { key: 'classLetter', label: t('colClass'),       num: true,  width: '4rem' },
      { key: 'looMinR',     label: t('colLOORange'),    num: true,  help: 'helpLOORange' },
      { key: 'singleMinR',  label: t('colSingleRange'), num: true,  help: 'helpSingleRange' },
      { key: 'gmina',       label: t('colGmina'),       num: false },
      { key: 'powiat',      label: t('colPowiat'),      num: false },
    ];
  }

  function levelLabel(level) {
    return t('level' + level[0].toUpperCase() + level.slice(1));
  }

  // Exactly what the region files carry — no street/town/public/n_years, and no
  // LOO or single-year folds (regions ship the `base` view alone).
  function regionColumns() {
    const parentLevel = PARENT_LEVEL[state.level];
    // Null-safe: currentColumnKeys() calls this during a level change, before the
    // new level's file has landed. Only the help strings read these.
    const loaded = populationLoaded() ? regionData : null;
    const nRanked = loaded ? loaded.regions.n_ranked[state.metric][state.subject] : null;
    const minPct = loaded ? loaded.metadata.min_percentile_n : null;
    const cols = [
      { key: 'rank', label: t('colRankNational'), num: true, width: '6rem',
        help: 'helpRankNational', helpArgs: [nRanked] },
      { key: 'name', label: levelLabel(state.level), num: false },
    ];
    if (parentLevel) cols.push({ key: 'parent', label: levelLabel(parentLevel), num: false });
    cols.push(
      { key: 'n_schools',   label: t('colNSchools'),  num: true, width: '5rem' },
      { key: 'n_students',  label: t('colNStudents'), num: true, width: '6rem' },
      { key: 'score',       label: t('colScore'),     num: true },
      { key: 'classLetter', label: t('colClass'),     num: true, width: '4rem' },
      { key: 'pct', label: t(PCT_COLUMN[state.level]), num: true,
        help: 'helpPctInParent', helpArgs: [minPct] },
    );
    return cols;
  }

  function currentColumns() {
    return state.level === 'school' ? schoolColumns() : regionColumns();
  }

  function currentColumnKeys() {
    return currentColumns().map(c => c.key);
  }

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
  function renderDetailRow(row, colspan) {
    const hist = shardHistory?.schools?.[String(row.rspo)];
    if (!hist) {
      return `<tr class="detail-row"><td colspan="${colspan}">
        <span class="muted small">${t(populationError ? 'historyFailed' : 'historyLoading')}</span>
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

  function classCellHTML(r) {
    return r.classLetter
      ? `<span class="class-badge" style="background:${r.classColour};color:${r.classTextColour}">${r.classLetter}</span>`
      : '—';
  }

  function schoolRowHTML(r, colspan) {
    const offMap = !r.hasCoords ? ` <span class="off-map" title="${t('offMap')}">📍✗</span>` : '';
    const looCell = (r.looMinR != null) ? `${r.looMinR}–${r.looMaxR}` : '—';
    const syCell  = (r.singleMinR != null) ? `${r.singleMinR}–${r.singleMaxR}` : '—';
    const pubLabel = r.pub ? t('publicYesShort') : t('publicNoShort');
    const selected = (r.rspo === state.selectedSchool);
    const mainRow = `<tr data-rspo="${r.rspo}"${selected ? ' class="highlight"' : ''}>
        <td class="num">${r.rank ?? '—'}</td>
        <td>${escapeHTML(r.name)}${offMap}</td>
        <td>${escapeHTML(r.town || '')}</td>
        <td>${escapeHTML(r.street || '')}</td>
        <td>${pubLabel}</td>
        <td class="num">${r.n_years}${r.n_years < 3 ? ` <span class="warn-icon" title="${escapeHTML(t('warnShortHistory'))}">⚠️</span>` : ''}</td>
        <td class="num">${fmtScoreHTML(r.score, state.metric)}</td>
        <td class="num class-cell">${classCellHTML(r)}</td>
        <td class="num">${looCell}</td>
        <td class="num">${syCell}</td>
        <td>${escapeHTML(r.gmina || '')}</td>
        <td>${escapeHTML(r.powiat || '')}</td>
      </tr>`;
    // A selected row expands an inline detail panel below it.
    return mainRow + (selected ? renderDetailRow(r, colspan) : '');
  }

  // No data-rspo, so the row-click handler below never binds to a region — the
  // detail panel is a per-school affordance and the region files carry nothing
  // to put in it.
  function regionRowHTML(r, hasParent) {
    // The reason rides in the name cell, not the score cell: at gmina level 69
    // rows carry it, and a sentence in a numeric column would set that column's
    // width for all 2,479.
    const reason = r.suppressed
      ? ` <span class="muted small">${escapeHTML(t(r.reasonKey))}</span>` : '';
    return `<tr data-region="${r.key}">
        <td class="num">${r.rank ?? '—'}</td>
        <td>${escapeHTML(r.name)}${reason}</td>
        ${hasParent ? `<td>${escapeHTML(r.parent || '')}</td>` : ''}
        <td class="num">${r.n_schools}</td>
        <td class="num">${r.n_students}</td>
        <td class="num">${fmtScoreHTML(r.score, state.metric)}</td>
        <td class="num class-cell">${classCellHTML(r)}</td>
        <td class="num">${r.pct == null ? '—' : r.pct.toFixed(1)}</td>
      </tr>`;
  }

  function renderTable(rows) {
    const table = document.getElementById('ranking-table');
    const columns = currentColumns();
    const head = `<thead><tr>${columns.map(col => {
      const indicator = (state.sortKey === col.key)
        ? `<span class="sort-indicator">${state.sortDir === 'asc' ? '▲' : '▼'}</span>` : '';
      const style = col.width ? ` style="width:${col.width};"` : '';
      const help = col.help
        ? ` <span class="help-icon" tabindex="0" role="button" aria-label="?" data-help="${escapeHTML(t(col.help, ...(col.helpArgs || [])))}">i</span>`
        : '';
      return `<th data-col="${col.key}" class="${col.num ? 'num' : ''}"${style}>${col.label}${help}${indicator}</th>`;
    }).join('')}</tr></thead>`;

    const schools = state.level === 'school';
    const hasParent = !!PARENT_LEVEL[state.level];
    const body = `<tbody>${rows.map(r =>
      schools ? schoolRowHTML(r, columns.length) : regionRowHTML(r, hasParent)
    ).join('')}</tbody>`;

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

  // Every row the current level ranks, before filtering. Regions are mapped from
  // the columnar payload by index rather than materialised up front, so a metric
  // or subject change re-reads the same loaded file instead of refetching it.
  function allRows() {
    if (!populationLoaded()) return [];
    if (state.level === 'school') return schoolRows.map(buildSchoolRow);
    return regionData.regions.teryt.map((_, i) => buildRegionRow(i));
  }

  // The denominator for "N of M". The level's whole population — for schools the
  // SELECTED POWIAT's school count, never the 12,889 nationally: claiming the
  // larger number is precisely the partial-ranking-as-a-whole this level control
  // exists to prevent.
  function populationSize() {
    if (!populationLoaded()) return 0;
    return state.level === 'school' ? schoolRows.length : regionData.regions.teryt.length;
  }

  // What stands in for the table when there is nothing honest to rank yet.
  // Returns the message, or '' when the table itself should render.
  function blockingMessage() {
    if (state.level === 'school' && !state.region) return t('rankingPickRegion');
    if (populationError) return t('historyFailed');
    if (!populationLoaded()) return t('historyLoading');
    return '';
  }

  function renderAll() {
    const promptEl = document.getElementById('ranking-prompt');
    const infoEl   = document.getElementById('ranking-info');
    const table    = document.getElementById('ranking-table');

    const blocked = blockingMessage();
    promptEl.textContent = blocked;
    promptEl.style.display = blocked ? '' : 'none';
    if (blocked) {
      infoEl.textContent = '';
      table.innerHTML = '';       // headers over an empty body just read as broken
      return;
    }

    const filtered = sortRows(filterRows(allRows()));
    infoEl.textContent = t(ROWS_SHOWN[state.level], filtered.length, populationSize());
    renderTable(filtered);
    if (state.level === 'school' && state.selectedSchool != null) {
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
    // Regions ship the `base` view alone, so the whole view machinery is a
    // school-level affordance (#level-note says so beside the disabled select).
    if (state.level !== 'school') { wrap.style.display = 'none'; return; }
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
  // Loading the population the table ranks
  //
  // One fetch per level change, metric change (school level only — the region
  // files carry every metric) or region change. Every loader in app.js caches
  // its promise, so revisiting a level or powiat costs nothing.
  //
  // At school level the shard doubles as the history file: it already carries
  // base/loo/single_year/last_k per subject, which is what the rank-range
  // columns, the non-base views and the detail panel need. There is no second
  // download the way there was when those lived in whole-country per-metric
  // files.

  async function fetchPopulation(level, region, metric) {
    if (REGION_LEVELS.includes(level)) {
      const parentLevel = PARENT_LEVEL[level];
      // The parent file is fetched only for its names (the "parent" column) —
      // 13 KB for voivodeship, 242 KB for powiat, both cached for the session.
      const [regions] = await Promise.all([
        loadRegions(level),
        parentLevel ? loadRegions(parentLevel) : Promise.resolve(null),
      ]);
      return { regions, schools: [], history: null };
    }
    if (!region) return { regions: null, schools: [], history: null };
    const [shard, index] = await Promise.all([
      loadShard(region, metric),
      loadIndex(),
      // Not used directly — it primes NAME_CACHE['gmina'], which is what lets
      // nameOf() resolve the gmina column and makes the name search match on it.
      // Same deal as map.js's buildSchools: one 1.59 MB fetch a session, and
      // only once a powiat has been deliberately chosen.
      loadRegions('gmina'),
      loadRegions('powiat'),
    ]);
    const col = index.schools;
    const pos = new Map(col.rspo.map((r, i) => [String(r), i]));
    const schools = Object.entries(shard.schools).map(([rspo, byLevel]) => {
      const i = pos.get(rspo);
      if (i == null) return null;
      const byMetric = byLevel[baselineLevel] || {};
      // The shape buildSchoolRow expects: school.scores[metric][subject].score.
      // `views.base` is legitimately absent for some cells (suppression is per
      // view), and assigning `undefined` here is correct — the row then shows
      // "—" rather than throwing. Do NOT rewrite as `views.base.score`.
      const scores = { [metric]: {} };
      for (const [subject, views] of Object.entries(byMetric)) {
        scores[metric][subject] = views.base;
      }
      return {
        rspo: Number(rspo),
        name: col.name[i],
        miejscowosc: col.miejscowosc[i],
        ulica_nr: col.ulica_nr[i],
        gmina: nameOf('gmina', col.teryt[i].slice(0, 6)),
        powiat: nameOf('powiat', col.powiat[i]),
        is_public: col.is_public[i],
        n_years: col.n_years[i],
        lat: col.lat[i],
        lon: col.lon[i],
        scores,
      };
    }).filter(Boolean);
    const history = { schools: Object.fromEntries(
      Object.entries(shard.schools).map(([rspo, byLevel]) => [rspo, byLevel[baselineLevel] || {}])
    ) };
    return { regions: null, schools, history };
  }

  // Request the current level's population and re-render when it lands. Never
  // awaited by a handler, so the page stays interactive for the whole download;
  // a response the user has already navigated away from is discarded by the key
  // check rather than clobbering what is on screen.
  function refreshPopulation() {
    const key = populationKey();
    const { level, region, metric } = state;
    populationError = false;
    if (loadedKey !== key) renderAll();   // show the loading state at once
    fetchPopulation(level, region, metric).then((got) => {
      if (key !== populationKey()) return;
      regionData = got.regions;
      schoolRows = got.schools;
      shardHistory = got.history;
      loadedKey = key;
      renderAll();
    }).catch((e) => {
      if (key !== populationKey()) return;
      // Say so rather than leaving "Ładowanie…" on screen forever.
      console.error('population load failed', e);
      populationError = true;
      renderAll();
    });
  }

  // ---------------------------------------------------------------------------
  // Controls wiring

  function fillLevelSelect(selectEl) {
    selectEl.innerHTML = '';
    for (const level of RANK_LEVELS) {
      const opt = document.createElement('option');
      opt.value = level;
      opt.textContent = levelLabel(level);
      if (level === state.level) { opt.selected = true; opt.setAttribute('selected', ''); }
      selectEl.appendChild(opt);
    }
  }

  // 380 powiats in <optgroup>s by voivodeship. Filled lazily — only once school
  // level is actually in play — because at a region level the two files behind
  // it may not be needed at all.
  let regionSelectFilled = false;

  async function fillRegionSelect() {
    const sel = document.getElementById('region-select');
    const [voiv, pow] = await Promise.all([loadRegions('voivodeship'), loadRegions('powiat')]);
    sel.innerHTML = '';
    const placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = t('regionPlaceholder');
    sel.appendChild(placeholder);
    const groups = new Map();
    const v = voiv.regions, p = pow.regions;
    for (let i = 0; i < v.teryt.length; i++) {
      const group = document.createElement('optgroup');
      group.label = v.name[i];
      groups.set(v.teryt[i], group);
      sel.appendChild(group);
    }
    for (let i = 0; i < p.teryt.length; i++) {
      const group = groups.get(p.parent[i]);
      if (!group) continue;
      const opt = document.createElement('option');
      opt.value = p.teryt[i];
      opt.textContent = p.name[i];
      group.appendChild(opt);
    }
    regionSelectFilled = true;
    sel.value = state.region || '';
    // A URL or stored TERYT that names no real powiat leaves the select on the
    // placeholder; drop it from state too, so the prompt appears instead of the
    // page silently ranking nothing.
    if (sel.value !== (state.region || '')) {
      state.region = null;
      writePref('rank_region', null);
      syncURL();
    }
  }

  // Level and region only mean something together; everything below them is a
  // school-level affordance. Disabled with a reason rather than hidden — the map
  // handles its own inapplicable baseline selector the same way.
  function syncControlAvailability() {
    const schools = state.level === 'school';
    document.getElementById('region-select').disabled = !schools;
    document.getElementById('view-select').disabled = !schools;
    for (const r of document.querySelectorAll('input[name="public"]')) r.disabled = !schools;
    document.getElementById('level-note').textContent = schools ? '' : t('levelRegionNote');
    const hint = document.querySelector('.click-hint');
    if (hint) hint.style.display = schools ? '' : 'none';
  }

  function wireControls() {
    const levelSel   = document.getElementById('level-select');
    const regionSel  = document.getElementById('region-select');
    const metricSel  = document.getElementById('metric-select');
    const subjectSel = document.getElementById('subject-select');
    const viewSel    = document.getElementById('view-select');
    const viewParamSel = document.getElementById('view-param-select');
    const nameInput  = document.getElementById('name-search');

    fillLevelSelect(levelSel);
    fillMetricSelect(metricSel,   state.metric, state.advancedMetrics);
    fillSubjectSelect(subjectSel, state.subject);
    viewSel.value = state.view;

    levelSel.addEventListener('change', () => {
      state.level = levelSel.value;
      writePref('rank_level', state.level);
      // The two shapes share only rank/name/score/classLetter, so a sort on a
      // column the new level does not have would silently order by nothing.
      if (!currentColumnKeys().includes(state.sortKey)) {
        state.sortKey = 'rank';
        state.sortDir = 'asc';
      }
      if (state.level === 'school' && !regionSelectFilled) {
        fillRegionSelect().catch(e => console.error('region list unavailable', e));
      }
      syncControlAvailability();
      updateViewParamField();
      syncURL();
      refreshPopulation();
    });

    regionSel.addEventListener('change', () => {
      state.region = regionSel.value || null;
      writePref('rank_region', state.region);
      state.selectedSchool = null;   // that school is not in the new powiat
      syncURL();
      refreshPopulation();
    });

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
        refreshPopulation();
      }
      fillMetricSelect(metricSel, state.metric, state.advancedMetrics);
      syncURL();
      renderAll();
    });

    metricSel.addEventListener('change', () => {
      state.metric = metricSel.value;
      writePref('metric', state.metric);
      syncURL();
      // The shards are per-metric, so school level needs a new one. The region
      // files carry every metric, so there it is a cache hit and renderAll alone
      // would do — refreshPopulation short-circuits to exactly that.
      refreshPopulation();
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
      // No fetch: the shard already carries loo/single_year/last_k alongside base.
      renderAll();
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
      fillLevelSelect(levelSel);
      fillMetricSelect(metricSel, state.metric, state.advancedMetrics);
      fillSubjectSelect(subjectSel, state.subject);
      // Only the placeholder is translated; the 380 powiat names are not.
      if (regionSelectFilled) {
        const placeholder = regionSel.querySelector('option[value=""]');
        if (placeholder) placeholder.textContent = t('regionPlaceholder');
      }
      syncControlAvailability();
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
    // The map hands a school over as ranking.html?school=<rspo> and carries no
    // region with it. Deriving the powiat from the index keeps that handoff
    // landing on the school instead of on the pick-a-county prompt.
    if (state.level === 'school' && !state.region && state.selectedSchool != null) {
      const col = indexData.schools;
      const i = col.rspo.indexOf(state.selectedSchool);
      if (i >= 0) state.region = col.powiat[i];
    }

    wireControls();
    syncControlAvailability();
    if (state.level === 'school') {
      fillRegionSelect().catch(e => console.error('region list unavailable', e));
    }
    fillDataYears();
    updateViewParamField();

    syncURL();
    // Unawaited, so the page stays usable for the whole download; renderAll runs
    // inside it, first for the loading state and again when the data lands.
    refreshPopulation();
  }

  main();
})();
