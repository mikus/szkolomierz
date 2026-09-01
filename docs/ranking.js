// Ranking page: a sortable/filterable table of whatever the level control picks
// — voivodeships, powiats, gminas or schools — by (metric, subject, view).
//
// An optional AREA FILTER (state.region) narrows the table to one voivodeship,
// powiat or gmina. It is a TERYT prefix — 2, 4 or 6 digits — and because those
// codes nest, the whole cascade is one `teryt.startsWith(prefix)` test and one
// piece of state; the three selects are a view of that single string, so they
// cannot disagree with each other.
//
// The four levels are deliberately NOT symmetric:
//   voivodeship / powiat / gmina  rank NATIONALLY, straight from
//     regions-{level}.json. Those files are 2.4 / 44 / 254 KB gzipped and
//     already whole-country, so the twenty best gminas in Poland cost nothing,
//     and narrowing to an area costs nothing either — it filters rows already
//     in memory and fetches not one byte.
//   school                        ranks WITHIN THE SELECTED AREA, from the
//     shard of every powiat it covers (docs/data/powiat/{teryt4}-{metric}.json).
//     One powiat is one file; a whole voivodeship is up to 42 of them and
//     ~3.4 MB gzipped on the primary metric. A NATIONAL school ranking would be
//     380 files and ~25 MB — exactly the payload the per-powiat sharding exists
//     to avoid — so with no area chosen the page shows a prompt rather than
//     quietly ranking one area's worth and calling it a ranking.
//
// Where an area is selected the table carries TWO rank columns. The published
// rank is national and stays that way in its own column; the second is computed
// here over the selected rows. Two denominators, each named in its own header —
// never one number that quietly changes meaning when a filter moves.

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
  // TERYT prefix length per selection depth, and the inverse. 6 not 7: the 7th
  // digit is the gmina TYPE, which 191-208 schools change between years, so it
  // is not part of a gmina's identity (see teryt.py).
  const SELECT_DIGITS = { voivodeship: 2, powiat: 4, gmina: 6 };
  const SELECT_LEVEL_BY_DIGITS = { 2: 'voivodeship', 4: 'powiat', 6: 'gmina' };
  // How deep the area filter may go at each ranking level. Selecting deeper than
  // the rows being ranked cannot narrow them — a 4-digit powiat TERYT never
  // starts with a 6-digit gmina prefix — it can only empty the table.
  const MAX_SELECT_DIGITS = { voivodeship: 2, powiat: 4, gmina: 6, school: 6 };
  const RANK_IN_COLUMN = {
    voivodeship: 'colRankInVoivodeship',
    powiat: 'colRankInPowiat',
    gmina: 'colRankInGmina',
  };
  // Depth of the thing in a row, for comparison against the selection's depth.
  // Schools are 8 because they sit below every region level: any area selection
  // is strictly shallower than a school, so their local rank always means
  // something. Selecting AT the ranked level leaves one row ranked 1 of 1.
  const RANKED_DIGITS = { voivodeship: 2, powiat: 4, gmina: 6, school: 8 };

  const state = {
    level: 'school',              // one of RANK_LEVELS
    region: null,                 // TERYT prefix: 2, 4 or 6 digits, or null for all
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
  // {done, total} while a multi-shard area is downloading, else null. A single
  // powiat is one file and lands fast enough that a counter would only flicker.
  let shardProgress = null;
  // What is actually in memory, as a populationKey(). Guards two things at once:
  // a response that lost the race (regions-gmina.json is 0.77 MB and can land
  // after a 6.9 KB voivodeship file requested later), and rendering the previous
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

  // 2, 4 or 6 digits — voivodeship, powiat or gmina. Anything else (including the
  // 7-digit form with the gmina type on the end) is not a selection this page
  // can act on, so it is dropped rather than silently truncated to something the
  // user did not ask for.
  function resolveRegion() {
    const valid = (v) => /^\d{2}$|^\d{4}$|^\d{6}$/.test(v || '');
    const url = getURLParams().get('region');
    if (valid(url)) return url;
    const stored = readPrefs().rank_region;
    return valid(stored) ? stored : null;
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
      // Now meaningful at every level, not just school: at a region level it
      // filters rows of a file already in memory.
      region:     state.region,
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
  // A near-twin of map.js's siblingCountsFor. Sharing it would be a MOVE, not an
  // addition — app.js gains the function only if map.js loses its copy — and that
  // means editing a freshly landed file this task does not own, with no JS test
  // layer to catch a slip. Hence the copy. What will actually drift is not the
  // counting but the reason ladder built on it (buildRegionRow below vs
  // map.js's regionTooltip): change one branch and the map and the table start
  // explaining the same suppressed gmina differently.
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
      teryt: school.teryt,      // what the area filter prefix-matches against
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
      // Second arg names the reference level actually in force. This page has no
      // baseline control, so it is always DEFAULTS.baseline — but the map writes
      // its own choice to the same localStorage, which is exactly why the help
      // must not say "the selected reference point".
      { key: 'rank',       label: t('colRankNational'), num: true,  width: '6rem',
        help: 'helpRankNational', helpArgs: [null, levelLabel(baselineLevel)] },
      ...selectionRankColumn(),
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

  // The selection-rank column, or nothing at all when no area is selected — so an
  // unfiltered table looks exactly as it did before this control existed, rather
  // than carrying a column of numbers identical to the one beside it.
  function selectionRankColumn() {
    if (!selectionRankMeaningful()) return [];
    return [{
      key: 'rankInSel', label: t(RANK_IN_COLUMN[selectionLevel()]), num: true, width: '7rem',
      help: 'helpRankInSelection', helpArgs: [selectionName()],
    }];
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
      ...selectionRankColumn(),
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

  // Same predicate as selectionRankColumn(), deliberately: the two must agree or
  // every cell after this one shifts a column left of its header.
  function selectionRankCellHTML(r) {
    if (!selectionRankMeaningful()) return '';
    return `<td class="num">${r.rankInSel ?? '—'}</td>`;
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
        ${selectionRankCellHTML(r)}
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
        ${selectionRankCellHTML(r)}
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

  function selectionLevel() {
    return state.region ? SELECT_LEVEL_BY_DIGITS[state.region.length] : null;
  }

  function selectionName() {
    const level = selectionLevel();
    return level ? nameOf(level, state.region) : '';
  }

  // A local rank says something only when the area holds more than one row of the
  // ranked level. Selecting a powiat while ranking powiats is a legitimate
  // lookup — it shows that one powiat's national standing — but its local rank
  // would read 1 for the single row, so the column is dropped rather than filled
  // with a constant.
  function selectionRankMeaningful() {
    const level = selectionLevel();
    return !!level && SELECT_DIGITS[level] < RANKED_DIGITS[state.level];
  }

  // Region rows carry their TERYT as `key`, school rows as `teryt`. Both are the
  // full code of the thing in the row, so one prefix test serves both.
  function rowTeryt(r) {
    return state.level === 'school' ? r.teryt : r.key;
  }

  // Rank within the selected area. Ties share the lowest rank, matching
  // rankdata(-x, 'min') in aggregate.py, so both rank columns break ties the same
  // way. A row with no score gets null: it holds no national rank either, and
  // inventing a local position for it would contradict that.
  function assignSelectionRank(rows) {
    const scored = rows.filter(r => r.score != null).sort((a, b) => b.score - a.score);
    for (let i = 0; i < scored.length;) {
      let j = i;
      while (j + 1 < scored.length && scored[j + 1].score === scored[i].score) j += 1;
      for (let k = i; k <= j; k += 1) scored[k].rankInSel = i + 1;
      i = j + 1;
    }
    for (const r of rows) if (r.score == null) r.rankInSel = null;
  }

  // Every row the current level ranks inside the selected area, before the name
  // and school-type filters. Regions are mapped from the columnar payload by
  // index rather than materialised up front, so a metric or subject change
  // re-reads the same loaded file instead of refetching it.
  //
  // This is deliberately the population BOTH the selection rank and the "N of M"
  // denominator use. Typing in the search box must not renumber a rank — the
  // national rank in the next column does not move when a filter does, and two
  // adjacent rank columns disagreeing about whether filters count would be
  // indefensible. And M is the selected area's size, never the 12,889 nationally:
  // claiming the larger number is precisely the partial-ranking-as-a-whole that
  // the level control and the pick-an-area prompt exist to prevent.
  function selectionRows() {
    if (!populationLoaded()) return [];
    const rows = state.level === 'school'
      ? schoolRows.map(buildSchoolRow)
      : regionData.regions.teryt.map((_, i) => buildRegionRow(i));
    if (!state.region) return rows;
    const inArea = rows.filter(r => rowTeryt(r).startsWith(state.region));
    assignSelectionRank(inArea);
    return inArea;
  }

  // What stands in for the table when there is nothing honest to rank yet.
  // Returns the message, or '' when the table itself should render.
  // The two messages differ per level because the two files differ. At school
  // level the thing being fetched genuinely IS the year-by-year data (the shard
  // carries loo/single_year/last_k); at a region level it is regions-{level}.json,
  // which has no yearly views at all, so borrowing the school copy would tell a
  // reader their yearly data failed when nothing yearly was ever requested.
  function blockingMessage() {
    if (state.level === 'school' && !state.region) return t('rankingPickRegion');
    const schools = state.level === 'school';
    if (populationError) return t(schools ? 'historyFailed' : 'regionsFailed');
    if (!populationLoaded()) {
      // Name the real cost while it is being paid: a whole voivodeship is up to
      // 42 files, and an unexplained pause that long reads as a broken page.
      if (schools && shardProgress) return t('shardsLoading', shardProgress.done, shardProgress.total);
      return t(schools ? 'historyLoading' : 'regionsLoading');
    }
    return '';
  }

  function renderAll() {
    const promptEl = document.getElementById('ranking-prompt');
    const infoEl   = document.getElementById('ranking-info');
    const table    = document.getElementById('ranking-table');

    const blocked = blockingMessage();
    promptEl.textContent = blocked;
    promptEl.style.display = blocked ? '' : 'none';
    // "Click a row to expand" only makes sense with school rows on screen — not
    // at a region level, and not under the pick-a-county prompt either.
    const hint = document.querySelector('.click-hint');
    if (hint) hint.style.display = (state.level === 'school' && !blocked) ? '' : 'none';
    if (blocked) {
      infoEl.textContent = '';
      table.innerHTML = '';       // headers over an empty body just read as broken
      return;
    }

    const inArea = selectionRows();
    const filtered = sortRows(filterRows(inArea));
    infoEl.textContent = t(ROWS_SHOWN[state.level], filtered.length, inArea.length);
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
  // its promise, so revisiting a level or an area costs nothing, and narrowing
  // from a voivodeship to one of its powiats refetches nothing at all.
  //
  // At school level the shard doubles as the history file: it already carries
  // base/loo/single_year/last_k per subject, which is what the rank-range
  // columns, the non-base views and the detail panel need. There is no second
  // download the way there was when those lived in whole-country per-metric
  // files.

  // Which powiat shards cover a selection. A powiat or gmina prefix names
  // exactly one (a gmina lies inside one powiat, so its first four digits are
  // the file); a voivodeship prefix names every powiat under it — up to 42.
  function powiatsFor(region, powiatRegions) {
    if (region.length >= 4) return [region.slice(0, 4)];
    return powiatRegions.regions.teryt.filter(t => t.startsWith(region));
  }

  async function fetchPopulation(level, region, metric, onProgress) {
    if (REGION_LEVELS.includes(level)) {
      const parentLevel = PARENT_LEVEL[level];
      // The parent file is fetched only for its names (the "parent" column) —
      // 6.9 KB for voivodeship, 118 KB for powiat, both cached for the session.
      const [regions] = await Promise.all([
        loadRegions(level),
        parentLevel ? loadRegions(parentLevel) : Promise.resolve(null),
      ]);
      return { regions, schools: [], history: null };
    }
    if (!region) return { regions: null, schools: [], history: null };
    // Awaited before the fan-out rather than alongside it: which shards to ask
    // for is derived from this file. 118 KB, and cached for the session.
    const powiatRegions = await loadRegions('powiat');
    const powiats = powiatsFor(region, powiatRegions);
    const [shards, index] = await Promise.all([
      loadShards(powiats, metric, onProgress),
      loadIndex(),
      // Not used directly — it primes NAME_CACHE['gmina'], which is what lets
      // nameOf() resolve the gmina column and makes the name search match on it.
      // Same deal as map.js's buildSchools: one 0.77 MB fetch a session, and
      // only once a powiat has been deliberately chosen.
      loadRegions('gmina'),
    ]);
    const col = index.schools;
    const pos = new Map(col.rspo.map((r, i) => [String(r), i]));
    // Not `baselineLevel` directly: a mean/median shard carries one level, not
    // four copies of it, so the file is what says which block to read (§2d).
    // `refLevel`, not `level`: this function's `level` is the ranking level.
    // Every shard of one metric carries the same level, so the first answers for
    // all of them.
    const refLevel = shardLevel(shards[0]);
    // The shards partition the country by powiat, so no rspo can appear in two
    // of them and concatenating them cannot collide.
    const entries = shards.flatMap(s => Object.entries(s.schools));
    const schools = entries.map(([rspo, byLevel]) => {
      const i = pos.get(rspo);
      if (i == null) return null;
      const byMetric = byLevel[refLevel] || {};
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
        // The school's own TERYT, which the area filter prefix-matches against.
        teryt: col.teryt[i],
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
      entries.map(([rspo, byLevel]) => [rspo, byLevel[refLevel] || {}])
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
    shardProgress = null;
    if (loadedKey !== key) renderAll();   // show the loading state at once
    // Only repaints the blocked page's one line of text, so running it per shard
    // is cheap. Guarded on the key: a superseded request must not write its
    // progress over the message of the one that replaced it.
    const onProgress = (done, total) => {
      if (key !== populationKey() || total < 2) return;
      shardProgress = { done, total };
      renderAll();
    };
    fetchPopulation(level, region, metric, onProgress).then((got) => {
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

  // The three area selects are a VIEW of the single state.region prefix, never
  // their own state. Each sync reads that one string and shows what it implies,
  // so they cannot drift apart from each other or from the table.

  function regionOption(value, label, i18nKey) {
    const opt = document.createElement('option');
    opt.value = value;
    opt.textContent = label;
    // Carries data-i18n like its static twin in the HTML, so applyI18N
    // retranslates it on a language switch and no hand-written patch is needed.
    if (i18nKey) opt.setAttribute('data-i18n', i18nKey);
    return opt;
  }

  // `regions` of null fills the placeholder alone — which is what a select whose
  // parent is unchosen should show, rather than all 2,479 gminas in Poland.
  function fillRegionOptions(sel, placeholderKey, regions, prefix) {
    sel.innerHTML = '';
    sel.appendChild(regionOption('', t(placeholderKey), placeholderKey));
    if (!regions) return;
    const r = regions.regions;
    const rows = [];
    for (let i = 0; i < r.teryt.length; i++) {
      if (prefix && !r.teryt[i].startsWith(prefix)) continue;
      rows.push([r.teryt[i], r.name[i]]);
    }
    // The files are ordered by TERYT; a person reading a dropdown wants names.
    rows.sort((a, b) => a[1].localeCompare(b[1], 'pl'));
    for (const [teryt, name] of rows) sel.appendChild(regionOption(teryt, name));
  }

  let regionSyncToken = 0;

  async function syncRegionSelects() {
    const token = ++regionSyncToken;
    const voivSel   = document.getElementById('voiv-select');
    const powiatSel = document.getElementById('powiat-select');
    const gminaSel  = document.getElementById('gmina-select');

    const maxDigits = MAX_SELECT_DIGITS[state.level];
    const sel    = state.region || '';
    const voiv   = sel.slice(0, 2);
    const powiat = sel.length >= 4 ? sel.slice(0, 4) : '';
    const gmina  = sel.length >= 6 ? sel : '';

    const [voivRegions, powiatRegions] = await Promise.all([
      loadRegions('voivodeship'), loadRegions('powiat'),
    ]);
    if (token !== regionSyncToken) return;   // a later sync already repainted

    fillRegionOptions(voivSel, 'voivPlaceholder', voivRegions, null);
    voivSel.value = voiv;

    fillRegionOptions(powiatSel, 'powiatPlaceholderAll', voiv ? powiatRegions : null, voiv);
    powiatSel.value = powiat;
    powiatSel.disabled = !voiv || maxDigits < 4;

    // regions-gmina.json is 0.77 MB, so it is fetched only when a powiat is
    // actually chosen AND the ranking level is deep enough for a gmina to
    // narrow anything.
    const wantGminas = !!powiat && maxDigits >= 6;
    const gminaRegions = wantGminas ? await loadRegions('gmina') : null;
    if (token !== regionSyncToken) return;
    fillRegionOptions(gminaSel, 'gminaPlaceholderAll', gminaRegions, powiat);
    gminaSel.value = gmina;
    gminaSel.disabled = !wantGminas;

    // A URL or stored prefix naming a region that does not exist leaves its
    // select on the placeholder. Trim the selection back to the deepest part
    // that did resolve, so the page filters by something real or by nothing —
    // never by a code no region has.
    const resolved = gminaSel.value || powiatSel.value || voivSel.value || '';
    if (resolved !== sel) {
      state.region = resolved || null;
      writePref('rank_region', state.region);
      syncURL();
    }
  }

  // The one place the area filter changes; everything else reads it.
  function setRegion(prefix) {
    state.region = prefix || null;
    writePref('rank_region', state.region);
    state.selectedSchool = null;   // that school need not be in the new area
    // Sorting by a column that no longer exists would silently order by nothing —
    // the same trap the level control guards against. Checked after the
    // assignment above, since the column list depends on it.
    if (!currentColumnKeys().includes(state.sortKey)) {
      state.sortKey = 'rank';
      state.sortDir = 'asc';
    }
    syncRegionSelects().catch(e => console.error('region lists unavailable', e));
    syncURL();
    refreshPopulation();
  }

  // Disabled with a reason rather than hidden — the map handles its own
  // inapplicable baseline selector the same way. Two different reasons can apply
  // here, so the note carries whichever do: the view and school-type filters are
  // school-level affordances, and an area select deeper than the ranking level
  // could not narrow the rows, only empty them.
  function syncControlAvailability() {
    const schools = state.level === 'school';
    document.getElementById('view-select').disabled = !schools;
    for (const r of document.querySelectorAll('input[name="public"]')) r.disabled = !schools;
    const parts = [];
    if (!schools) parts.push(t('levelRegionNote'));
    if (MAX_SELECT_DIGITS[state.level] < 6) parts.push(t('selectDeeperThanLevel'));
    document.getElementById('level-note').textContent = parts.join(' ');
    // The click hint is NOT set here: it depends on whether rows are on screen,
    // which changes on area selection and on load too. renderAll owns it.
  }

  function wireControls() {
    const levelSel   = document.getElementById('level-select');
    const voivSel    = document.getElementById('voiv-select');
    const powiatSel  = document.getElementById('powiat-select');
    const gminaSel   = document.getElementById('gmina-select');
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
      // A selection deeper than the new level cannot narrow its rows, only empty
      // them, so it is trimmed back to the deepest level that still means
      // something rather than dropped outright.
      const maxDigits = MAX_SELECT_DIGITS[state.level];
      if (state.region && state.region.length > maxDigits) {
        state.region = state.region.slice(0, maxDigits);
        writePref('rank_region', state.region);
      }
      syncRegionSelects().catch(e => console.error('region lists unavailable', e));
      syncControlAvailability();
      updateViewParamField();
      syncURL();
      refreshPopulation();
    });

    // Clearing a select falls back to its parent rather than to nothing: a
    // powiat set to "whole voivodeship" means the voivodeship, not all of Poland.
    // Changing the voivodeship sets a 2-digit prefix, which by construction drops
    // whatever powiat and gmina were under the old one.
    voivSel.addEventListener('change',   () => setRegion(voivSel.value));
    powiatSel.addEventListener('change', () => setRegion(powiatSel.value || voivSel.value));
    gminaSel.addEventListener('change',  () => setRegion(gminaSel.value || powiatSel.value));

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
      // The placeholder carries data-i18n, so setLang's applyI18N already
      // retranslated it; the 380 powiat names are proper nouns and are not.
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
    // Awaited, unlike the population below it: this is what corrects a bogus
    // ?region= before anything is fetched for it, and the two files behind it are
    // 6.9 KB + 118 KB and needed at every level anyway.
    await syncRegionSelects().catch(e => console.error('region lists unavailable', e));
    fillDataYears();
    updateViewParamField();

    syncURL();
    // Unawaited, so the page stays usable for the whole download; renderAll runs
    // inside it, first for the loading state and again when the data lands.
    refreshPopulation();
  }

  main();
})();
