// Shared code for index.html (map) and ranking.html (table).
// - constants, colour mapping
// - data loading (base + per-metric on demand)
// - URL state + localStorage persistence (§9 of MAP_APP_BRIEF.md)
// - i18n translations

// -----------------------------------------------------------------------------
// Constants

const METRICS  = ['mean', 'median', 'diff_mean', 'unit_norm_diff_mean'];

// Only two metrics are offered by default; the other two sit behind the
// "advanced metrics" toggle. Not decluttering for its own sake — neither hidden
// metric tells a reader anything the visible pair doesn't:
//   diff_mean  ranks identically to unit_norm_diff_mean (Spearman 1.000), so
//              switching to it reorders nothing and only changes the numbers.
//   median     scored measurably worse than mean on the leave-one-out stability
//              test: the median moves the full exam-difficulty shift each year,
//              while the mean is damped by floor/ceiling effects.
// Both stay in the xlsx exports, where an analyst can use them deliberately.
//
// `mean` leads because it is the number people arrive expecting: a percentage
// per subject. unit_norm_diff_mean is the more defensible metric — it is the one
// the leave-one-out test picked, and it neutralises a year's exam difficulty —
// but it reads as an unlabelled decimal around zero, and a reader who cannot
// find "56%" anywhere concludes the page is broken rather than that it is
// precise. The stronger metric stays one click away.
const BASIC_METRICS = ['mean', 'unit_norm_diff_mean'];

function isAdvancedMetric(metric) {
  return !BASIC_METRICS.includes(metric);
}

// Advanced mode is on when the user ticked it, or when the URL asks for an
// advanced metric. A shared link must show the recipient what the sender saw —
// silently swapping in a different metric would be worse than briefly revealing
// a control they hadn't opted into.
function resolveAdvancedMetrics() {
  const url = getURLParams();
  if (url.get('advanced') === '1') return true;
  if (isAdvancedMetric(url.get('metric'))  && METRICS.includes(url.get('metric'))) return true;
  if (readPrefs().advanced_metrics) return true;
  const stored = readPrefs().metric;
  return !!stored && METRICS.includes(stored) && isAdvancedMetric(stored);
}

// Basic metrics keep their positions when the advanced ones are appended, so the
// default metric stays first in both modes instead of jumping down the list.
function visibleMetrics(advanced) {
  if (!advanced) return BASIC_METRICS;
  return [...BASIC_METRICS, ...METRICS.filter(m => !BASIC_METRICS.includes(m))];
}
const SUBJECTS = ['polski', 'matematyka', 'angielski', 'composite_min'];
const CORE_SUBJECTS = ['polski', 'matematyka', 'angielski'];

const DEFAULTS = {
  metric:   'mean',
  subject:  'composite_min',
  lang:     'pl',
  baseline: 'voivodeship',   // the level the map used before this change
};

const COLOURS = {
  satRed:   '#d6604d',
  red:      '#f4a582',
  yellow:   '#fde08a',
  green:    '#a6dba0',
  satGreen: '#1a9850',
  missing:  '#bbb',
};

const NOMINATIM_BASE = 'https://nominatim.openstreetmap.org/search';
// Mazowieckie viewbox: left,top,right,bottom (lon/lat).
const MAZ_VIEWBOX = '19.2,53.6,23.2,51.0';

// -----------------------------------------------------------------------------
// Colour / class mapping
//
// 3 classes by distance from the per-(metric,subject) centre, boundary ±0.33σ:
//   A = good   (z >  +0.33σ)
//   B = medium (±0.33σ — the "muddy middle"; kept flat yellow, honest)
//   C = weak   (z <  −0.33σ)
// Index 0=weak(C) … 2=good(A). The ±0.33σ band is wider than the multi-year
// base score's own noise (~0.12σ from LOO), so the three buckets are
// statistically distinguishable; the old ±1.5σ A/E split was arbitrary.

const CLASS_BOUND = 0.33;
const CLASS3_LETTERS = ['C', 'B', 'A'];
const CLASS3_FLAT = [COLOURS.satRed, COLOURS.yellow, COLOURS.satGreen];  // toggle-off

function classIndex3(score, centre, sigma) {
  if (score == null || sigma == null || sigma === 0) return null;
  const z = (score - centre) / sigma;
  if (z >  CLASS_BOUND) return 2;  // A good
  if (z < -CLASS_BOUND) return 0;  // C weak
  return 1;                        // B medium
}

function classLetter3(score, centre, sigma) {
  const i = classIndex3(score, centre, sigma);
  return i == null ? null : CLASS3_LETTERS[i];
}

function hexLerp(a, b, t) {
  const pa = [parseInt(a.slice(1, 3), 16), parseInt(a.slice(3, 5), 16), parseInt(a.slice(5, 7), 16)];
  const pb = [parseInt(b.slice(1, 3), 16), parseInt(b.slice(3, 5), 16), parseInt(b.slice(5, 7), 16)];
  const ch = pa.map((v, i) => Math.round(v + (pb[i] - v) * t).toString(16).padStart(2, '0'));
  return '#' + ch.join('');
}

// Dark or light text that reads on a given background colour. The 0.5 cutoff
// (rather than 0.6) keeps dark text on the medium greens/reds — where it
// actually has better contrast than white — so white letters appear only on the
// darkest backgrounds (a small elite top band, not the whole top ~5%).
function textOn(hex) {
  const r = parseInt(hex.slice(1, 3), 16), g = parseInt(hex.slice(3, 5), 16), b = parseInt(hex.slice(5, 7), 16);
  return (0.299 * r + 0.587 * g + 0.114 * b) / 255 > 0.5 ? '#222' : '#fff';
}

// Continuous colour: flat yellow in B (±0.33σ, muddy middle); A ramps
// yellow→green→satGreen out to p99; C ramps yellow→red→satRed out to p1;
// saturates beyond p1/p99. p1/p99 are the robust extremes (not min/max), so one
// outlier school can't stretch the scale and wash everyone else out.
function gradient3Colour(score, centre, sigma, p1, p99) {
  if (score == null || sigma == null || sigma === 0) return COLOURS.missing;
  const lo = centre - CLASS_BOUND * sigma;
  const hi = centre + CLASS_BOUND * sigma;
  if (score >= lo && score <= hi) return COLOURS.yellow;
  if (score > hi) {
    const t = Math.min(1, (score - hi) / Math.max(1e-9, p99 - hi));
    return t <= 0.5 ? hexLerp(COLOURS.yellow, COLOURS.green, t / 0.5)
                    : hexLerp(COLOURS.green, COLOURS.satGreen, (t - 0.5) / 0.5);
  }
  const t = Math.min(1, (lo - score) / Math.max(1e-9, lo - p1));
  return t <= 0.5 ? hexLerp(COLOURS.yellow, COLOURS.red, t / 0.5)
                  : hexLerp(COLOURS.red, COLOURS.satRed, (t - 0.5) / 0.5);
}

// gradient=true → continuous (gradient3Colour); false → 3 flat class colours.
function colourFor(score, centre, sigma, p1, p99, gradient) {
  const i = classIndex3(score, centre, sigma);
  if (i == null) return COLOURS.missing;
  return gradient ? gradient3Colour(score, centre, sigma, p1, p99) : CLASS3_FLAT[i];
}

const REFERENCE_LEVEL_KEYS = ['national', 'voivodeship', 'powiat', 'gmina'];

// What a SCHOOL is compared against (spec §6.2). Shared rather than per-page:
// index.html and ranking.html both load app.js and both colour by these anchors.
let baselineLevel = DEFAULTS.baseline;

// p1/p99 are precomputed nationally in scale.json. They used to be derived here
// by sorting every school's score, which with per-powiat shards would sort ~27
// schools and colour the same school differently depending which shard loaded
// first.
function scaleFor(metric, subject) {
  const byMetric = scaleData && scaleData.school[baselineLevel];
  return (byMetric && byMetric[metric] && byMetric[metric][subject])
    || { sigma: 1, sigma_centre: 0, p1: 0, p99: 0 };
}

function scoreExtent(metric, subject) {
  const { p1, p99 } = scaleFor(metric, subject);
  return { p1, p99 };
}

// Per-subject line colours, shared by the map popup sparkline and the ranking
// detail charts so a subject reads the same everywhere.
const SUBJECT_COLOURS = {
  polski: '#1f77b4',
  matematyka: '#d62728',
  angielski: '#2ca02c',
  composite_min: '#7f7f7f',
};

// -----------------------------------------------------------------------------
// Small multi-line chart with labelled axes (shared: map popup + ranking detail)
//
// opts:
//   years:   [2021, 2022, …]  — x positions, in order
//   series:  [{ colour, points: {<year>: value|null},
//              markers?: bool,      // line + dots
//              pointsOnly?: bool }] // dots only, no line (e.g. composite_min,
//                                   //   so it doesn't hide the subject lines)
//   invertY: true for rank charts (1 = best, drawn at the top)
//   fmtY:    (value) => short string, used for the Y-axis tick labels
//   width/height: optional pixel size
// Points keyed by year; missing years are gaps (line skips them).

function lineChartSVG(opts) {
  const W = opts.width || 210;
  const H = opts.height || 132;
  const mL = 38, mR = 16, mT = 8, mB = 18;      // margins for axis labels
  const years = opts.years;
  const plotW = W - mL - mR, plotH = H - mT - mB;

  const allY = [];
  for (const s of opts.series) {
    for (const y of years) {
      const v = s.points[y];
      if (v != null) allY.push(v);
    }
  }
  if (!allY.length) return `<svg width="${W}" height="${H}"></svg>`;

  let lo = Math.min(...allY), hi = Math.max(...allY);
  const dataLo = lo, dataHi = hi;   // real range, for the axis labels
  if (lo === hi) { lo -= 1; hi += 1; }
  const padv = (hi - lo) * 0.1;     // padded range, for plotting (breathing room)
  lo -= padv; hi += padv;

  const xOf = (year) => {
    const i = years.indexOf(year);
    return mL + (years.length === 1 ? plotW / 2 : (i / (years.length - 1)) * plotW);
  };
  // invertY: low value (rank 1) at the top; normal: high value at the top.
  const yOf = (v) => {
    const t = (v - lo) / (hi - lo);
    return opts.invertY ? (mT + t * plotH) : (mT + (1 - t) * plotH);
  };

  const fmtY = opts.fmtY || ((v) => String(Math.round(v)));

  // Y gridlines + tick labels: min, max, and a couple of values in between
  // for readability. Ticks sit at real data values (computed across the data
  // range, positioned via yOf which handles the invertY case).
  const nTicks = (dataHi === dataLo) ? 1 : 4;   // min, two intermediate, max
  let grid = '';
  for (let k = 0; k < nTicks; k++) {
    const v = (nTicks === 1) ? dataLo : dataLo + (k / (nTicks - 1)) * (dataHi - dataLo);
    const gy = yOf(v);
    grid +=
      `<line x1="${mL}" y1="${gy.toFixed(1)}" x2="${mL + plotW}" y2="${gy.toFixed(1)}" stroke="#eee"/>` +
      `<text x="${mL - 4}" y="${(gy + 3).toFixed(1)}" text-anchor="end" font-size="9" fill="#666">${fmtY(v)}</text>`;
  }

  const axes =
    `<line x1="${mL}" y1="${mT}" x2="${mL}" y2="${mT + plotH}" stroke="#ccc"/>` +
    `<line x1="${mL}" y1="${mT + plotH}" x2="${mL + plotW}" y2="${mT + plotH}" stroke="#ccc"/>`;

  const xLabels = years.map(y =>
    `<text x="${xOf(y).toFixed(1)}" y="${H - 5}" text-anchor="middle" font-size="9" fill="#666">${y}</text>`
  ).join('');

  const lines = opts.series.map(s => {
    const coords = years
      .map(y => (s.points[y] == null) ? null : `${xOf(y).toFixed(1)},${yOf(s.points[y]).toFixed(1)}`)
      .filter(Boolean);
    if (!coords.length) return '';
    const poly = s.pointsOnly
      ? ''
      : `<polyline fill="none" stroke="${s.colour}" stroke-width="${s.markers ? 2 : 1.4}" points="${coords.join(' ')}"/>`;
    const dots = (s.markers || s.pointsOnly)
      ? years.map(y => (s.points[y] == null) ? '' :
          `<circle cx="${xOf(y).toFixed(1)}" cy="${yOf(s.points[y]).toFixed(1)}" r="${s.pointsOnly ? 2.6 : 2.3}" fill="${s.colour}"/>`).join('')
      : '';
    return poly + dots;
  }).join('');

  return `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg">${grid}${axes}${xLabels}${lines}</svg>`;
}

// Shared subject legend (coloured dots + names), used under chart groups.
function subjectLegendHTML(subjects) {
  return subjects.map(s =>
    `<span class="legend-item" style="color:${SUBJECT_COLOURS[s]}">●&nbsp;${t('subject_' + s)}</span>`
  ).join(' ');
}

// -----------------------------------------------------------------------------
// Data loading

// One loader per artifact. The old loadBaseData was a single-shot memoised
// global with no merge path: a second fetch overwrote the first, which is fine
// for one whole-country file and wrong for 1,520 per-powiat shards. Each cache
// below is therefore keyed by what makes its payload distinct.

// regionCache and shardCache hold the PROMISE, not the resolved payload, for the
// same reason geoCache does: zoomend and moveend both fire in one interaction, so
// the same key can be requested twice before the first fetch lands. Caching the
// resolved value would let both requests through — 1.68 MB for a powiat shard,
// 1.59 MB for regions-gmina.json.
let indexData = null, scaleData = null;
const regionCache = new Map();   // level       -> Promise<payload>
const shardCache  = new Map();   // "1425|mean" -> Promise<payload>
const NAME_CACHE  = new Map();   // level       -> Map(teryt -> name)

async function loadIndex() {
  if (indexData) return indexData;
  const res = await fetch('data/schools-index.json');
  if (!res.ok) throw new Error(`schools-index.json: HTTP ${res.status}`);
  indexData = await res.json();
  return indexData;
}

async function loadScale() {
  if (scaleData) return scaleData;
  const res = await fetch('data/scale.json');
  if (!res.ok) throw new Error(`scale.json: HTTP ${res.status}`);
  scaleData = await res.json();
  return scaleData;
}

async function loadRegions(level) {
  if (!regionCache.has(level)) {
    regionCache.set(level, fetch('data/regions-' + level + '.json').then((r) => {
      if (!r.ok) throw new Error(`regions-${level}.json: HTTP ${r.status}`);
      return r.json();
    }).then((payload) => {
      const names = new Map();
      const { teryt, name } = payload.regions;
      for (let i = 0; i < teryt.length; i++) names.set(teryt[i], name[i]);
      NAME_CACHE.set(level, names);
      return payload;
    }));
  }
  return regionCache.get(level);
}

// Region display names, filled by loadRegions as each level lands. Here rather
// than in map.js because ranking.html loads only app.js and ranking.js.
// schools-index.json carries no gmina name — it has the TERYT code — so a
// school's gmina label ("gm. Gózd" in the typeahead, a ranking column, and the
// text the ranking query matches against) resolves through this. Falls back to
// the code, so a name that has not loaded yet degrades rather than blanks.
function nameOf(level, teryt) {
  const m = NAME_CACHE.get(level);
  return (m && m.get(teryt)) || teryt;
}

async function loadShard(powiat, metric) {
  const key = powiat + '|' + metric;
  if (!shardCache.has(key)) {
    shardCache.set(key, fetch('data/powiat/' + powiat + '-' + metric + '.json').then((r) => {
      if (!r.ok) throw new Error(`${powiat}-${metric}.json: HTTP ${r.status}`);
      return r.json();
    }));
  }
  return shardCache.get(key);
}

// Each geometry file is named for the region in view but contains its CHILDREN,
// so `level` here is the parent's level, not the level being drawn. There is no
// gmina branch because there is no geometry below gmina — at that level the map
// draws school markers instead. Caching the promise rather than the resolved
// value means two moveend events in the same tick share one fetch.
const geoCache = new Map();

async function loadGeometryFor(level, focused) {
  const path = level === 'country' ? 'geo/kraj.json'
    : level === 'voivodeship' ? `geo/woj/${focused.slice(0, 2)}.json`
    : `geo/pow/${focused.slice(0, 4)}.json`;
  if (!geoCache.has(path)) {
    geoCache.set(path, fetch(path).then((r) => {
      if (!r.ok) throw new Error(`no geometry at ${path}`);
      return r.json();
    }).catch((e) => {
      // Drop the failed promise before rethrowing. Caching the REJECTION would
      // hand the same failure to every later caller for this path, and geometry
      // is fetched from zoomend/moveend — where panning away and back IS the
      // natural retry. Without this, one transient blip leaves that region
      // permanently unrendered for the rest of the session, silently. The
      // caller still sees the error; only the cache entry goes.
      // loadRegions and loadShard deliberately keep their cached rejections:
      // they fire rarely and on deliberate action, not on every gesture.
      geoCache.delete(path);
      throw e;
    }));
  }
  return geoCache.get(path);
}

// -----------------------------------------------------------------------------
// URL state + localStorage persistence

const STORAGE_KEY = 'schools-app-prefs';

function readPrefs() {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}');
  } catch {
    return {};
  }
}

function writePref(key, value) {
  const prefs = readPrefs();
  if (value == null) delete prefs[key]; else prefs[key] = value;
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(prefs)); } catch {}
}

function getURLParams() {
  return new URLSearchParams(window.location.search);
}

function setURLParams(params) {
  const usp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v == null || v === '' || v === false) continue;
    usp.set(k, String(v));
  }
  const qs = usp.toString();
  const url = window.location.pathname + (qs ? '?' + qs : '') + window.location.hash;
  window.history.replaceState(null, '', url);
}

// Pick a value from URL > storage > default, validating against allowed list.
function resolvePref(name, allowed) {
  const url = getURLParams().get(name);
  if (url && (!allowed || allowed.includes(url))) return url;
  const stored = readPrefs()[name];
  if (stored && (!allowed || allowed.includes(stored))) return stored;
  return DEFAULTS[name];
}

// -----------------------------------------------------------------------------
// i18n

const I18N = {
  pl: {
    appTitle: 'Mapa szkół podstawowych — Mazowieckie',
    navMap: 'Mapa',
    navRanking: 'Ranking',
    navHelp: 'Pomoc',
    breadcrumbPoland: 'Polska',
    helpTitle: 'Pomoc',
    helpLink: 'Jak liczone są wyniki? → Pomoc',
    tocFabLabel: 'Do spisu treści',
    sectionView: 'Widok',
    sectionFilters: 'Filtry',
    sectionFindSchool: 'Znajdź szkołę',
    findSchoolPlaceholder: 'np. Słupica, STO, Kopernika',
    findSchoolHelp: 'Wpisz nazwę lub miejscowość — wybierz z listy, aby przejść do szkoły na mapie.',
    findSchoolNoResults: 'Brak pasujących szkół',
    findSchoolOffMap: '(brak na mapie)',
    sectionSearch: 'Szukaj adresu',
    sectionLegend: 'Legenda',
    sectionSettings: 'Ustawienia',
    gradientToggle: 'Gradient koloru',
    gradientHelp: 'Płynne przejście koloru w klasach A i C (im dalej od średniej, tym mocniej, aż do 1.–99. percentyla). Środek (B) pozostaje jednolicie żółty.',
    labelSubject: 'Przedmiot',
    labelMetric: 'Metryka',
    labelBaseline: 'Punkt odniesienia',
    // The four REFERENCE_LEVEL_KEYS. Deliberately not the same vocabulary as the
    // zoom levels: `country` is a zoom rung, `national` is what a school is
    // compared against.
    levelNational: 'Cała Polska',
    levelVoivodeship: 'Województwo',
    levelPowiat: 'Powiat',
    levelGmina: 'Gmina',
    labelPublic: 'Publiczna',
    publicAll: 'Wszystkie',
    publicYes: 'Tak',
    publicNo: 'Nie',
    labelThreshold: 'Min. wynik',
    labelMinYears: 'Min. liczba lat danych',
    searchPlaceholder: 'np. Marszałkowska 1, Warszawa',
    searchButton: 'Szukaj',
    searchHelp: 'Geokodowanie: OpenStreetMap Nominatim. Wyszukiwanie tylko po kliknięciu „Szukaj”.',
    searchNotFound: 'Nie znaleziono adresu.',
    searchError: 'Błąd geokodowania.',
    legendGood: 'Powyżej średniej (> +0.33σ)',
    legendMedium: 'W okolicy średniej (±0.33σ)',
    legendWeak: 'Poniżej średniej (< −0.33σ)',
    legendGradientNote: 'W klasach A i C kolor jest ciągły — im dalej od średniej, tym mocniejszy, aż do 1. i 99. percentyla. Dlatego na mapie widać więcej odcieni niż trzy. Klasa B zostaje jednolicie żółta.',
    metric_mean: 'Średnia',
    metric_median: 'Mediana',
    metric_diff_mean: 'Różnica od średniej',
    metric_unit_norm_diff_mean: 'Wynik znormalizowany',
    subject_polski: 'Polski',
    subject_matematyka: 'Matematyka',
    subject_angielski: 'Angielski',
    subject_composite_min: 'Najsłabszy przedmiot',
    popupPublic: 'Publiczna',
    popupPrivate: 'Niepubliczna',
    popupYears: 'lat danych',
    popupScore: 'Wynik',
    popupRank: 'Miejsce',
    popupPct: 'Percentyl',
    helpPct: 'Ile procent szkół ma wynik nie lepszy niż ta szkoła. 50 znaczy, że połowa wypadła gorzej lub tak samo; 100 to najlepszy wynik w zestawieniu. Liczone wśród szkół obecnych w tym samym widoku.',
    popupComposite: 'Najsłabszy z 3',
    warnShortHistory: 'Krótka historia (< 3 lata) — wyniki mniej pewne.',
    warnVolatile: 'Duże wahania roczne — wynik zależy od wyboru lat.',
    rankingTitle: 'Ranking szkół — Mazowieckie',
    rankingNameSearch: 'Szukaj po nazwie lub lokalizacji',
    rankingSearchPlaceholder: 'np. STO, Vizja, Słupica',
    lastKRow: (k) => `ostatnie ${k}`,
    rankingView: 'Widok danych',
    rankingViewParam: 'Parametr widoku',
    rankingViewBase: 'wszystkie lata (base)',
    rankingViewLastK: 'ostatnie k lat (last_k)',
    rankingViewSingleYear: 'jeden rok (single_year)',
    rankingViewLOO: 'bez jednego roku (LOO)',
    colName: 'Szkoła',
    colStreet: 'Ulica',
    colTown: 'Miejscowość',
    colGmina: 'Gmina',
    colPowiat: 'Powiat',
    colPublic: 'Publiczna',
    colNYears: 'Lata',
    colRank: 'Miejsce',
    colLOORange: 'Zakres pozycji (LOO)',
    colSingleRange: 'Zakres pozycji (pojed. lata)',
    helpLOORange: 'Zakres miejsc w rankingu, gdy z obliczeń pominiemy po kolei każdy rok (jackknife „leave-one-out”). Szeroki zakres = pozycja mocno zależy od tego, który rok uwzględnimy.',
    helpSingleRange: 'Zakres miejsc w rankingu liczonych z każdego pojedynczego roku osobno. Szeroki zakres = duże wahania wyniku rok do roku.',
    colScore: 'Wynik',
    colClass: 'Klasa',
    clickHint: 'Kliknij wiersz, aby rozwinąć szczegóły szkoły.',
    detailClassByYear: (s) => `Klasa (${s}) po latach`,
    detailViewLOO: 'LOO',
    detailViewSingle: 'pojedyncze lata',
    detailShowTables: 'Pokaż tabele liczbowe',
    detailHideTables: 'Ukryj tabele liczbowe',
    detailSecSingle: 'Pojedyncze lata',
    detailSecLOO: 'LOO (z pominięciem roku)',
    detailSecLastK: 'Ostatnie k lat',
    detailSecBase: 'Wynik za całość lat (baza)',
    detailWeakestNote: 'Pogrubienie = przedmiot z najniższym wynikiem (ten, który wyznacza composite_min), niezależnie od pokazywanej miary (wynik/pozycja/percentyl).',
    offMap: 'brak lokalizacji',
    rowsShown: (n, total) => `${n} z ${total} szkół`,
    regionNoSchools: 'Brak szkół z wynikami egzaminu',
    // Not "the region is small": the score is withheld when the COMPARISON
    // population is too small — a difference metric needs siblings to measure
    // against, and an only child is its own reference (suppression.py).
    regionTooSmall: 'Za mała grupa odniesienia, aby policzyć wynik',
    // The other half of that gate, and on today's data the ONLY half that fires:
    // every suppressed region is the single gmina of a one-gmina powiat, whose
    // parent holds far more than MIN_REFERENCE_N schools. Saying "too small"
    // there is simply false.
    regionOnlyChild: 'Jedyna jednostka w jednostce nadrzędnej — nie ma z czym porównać',
    // Polish counts in three forms and 312 gminas hold exactly one school, so
    // a single fixed noun would read "1 szkół" on the tooltip of every one.
    schoolsInRegion: (n) => {
      const d = n % 10, dd = n % 100;
      const word = n === 1 ? 'szkoła'
        : (d >= 2 && d <= 4 && (dd < 12 || dd > 14)) ? 'szkoły' : 'szkół';
      return `${n} ${word}`;
    },
    dataYears: (lo, hi) => `Egzamin ósmoklasisty ${lo}–${hi}`,
    historyLoading: 'Ładowanie szczegółowych danych…',
    historyFailed: 'Nie udało się wczytać danych rocznych — odśwież stronę.',
    chartYearsCaption: 'Wynik w poszczególnych latach',
    helpPopupChart: 'Wykres pokazuje wynik policzony osobno dla każdego roku — to nie jest wynik zbiorczy za wszystkie lata ani wersja LOO. Wynik zbiorczy masz w tabeli powyżej.',
    helpMetric: 'Średnia to zwykły wynik procentowy — tyle procent punktów zdobyli przeciętnie uczniowie tej szkoły. Wynik znormalizowany to odległość od średniej województwa z tego samego roku: 0 oznacza dokładnie średnią, wartości dodatnie są powyżej niej, ujemne poniżej. Kliknij, aby przeczytać o wszystkich metrykach.',
    helpBaseline: 'Wynik szkoły to odległość od średniej grupy odniesienia. Ten wybór decyduje, jaka to grupa: cała Polska, województwo, powiat czy gmina. Im węższa grupa, tym bardziej wynik mówi „jak na tle najbliższej okolicy”, a mniej „jak na tle kraju”.',
    baselineFollowsZoom: 'Dotyczy tylko widoku szkół. Wyżej kolor regionu zawsze porównuje go z jednostką nadrzędną.',
    baselineRawMetric: 'Średnia i Mediana to surowy wynik procentowy — nie mają grupy odniesienia, więc ten wybór ich nie zmienia.',
    advancedMetrics: 'Metryki zaawansowane',
    advancedMetricsHelp: 'Dokłada „Mediana” i „Różnica od średniej”. Różnica od średniej ustawia szkoły w dokładnie tej samej kolejności co wynik znormalizowany — zmienia się tylko skala liczb.',
    chartDiffCaption: 'LOO a pojedyncze lata — jaka różnica?',
    helpChartDiff: 'Pojedyncze lata: każdy punkt policzony wyłącznie z tego jednego rocznika — pokazuje, jak wynik skacze rok do roku. LOO („leave-one-out”): każdy punkt to wynik za wszystkie lata z pominięciem tego jednego — punkty zmieniają się słabiej, bo każdy opiera się na pozostałych rocznikach. Duży rozrzut punktów LOO znaczy, że wynik szkoły mocno zależy od jednego rocznika.',
    publicYesShort: 'Tak',
    publicNoShort: 'Nie',
    langPL: 'PL',
    langEN: 'EN',
  },
  en: {
    appTitle: 'Primary schools map — Mazowieckie',
    navMap: 'Map',
    navRanking: 'Ranking',
    navHelp: 'Help',
    breadcrumbPoland: 'Poland',
    helpTitle: 'Help',
    helpLink: 'How are scores computed? → Help',
    tocFabLabel: 'To contents',
    sectionView: 'View',
    sectionFilters: 'Filters',
    sectionFindSchool: 'Find a school',
    findSchoolPlaceholder: 'e.g. Słupica, STO, Kopernika',
    findSchoolHelp: 'Type a name or town — pick from the list to jump to the school on the map.',
    findSchoolNoResults: 'No matching schools',
    findSchoolOffMap: '(not on map)',
    sectionSearch: 'Address search',
    sectionLegend: 'Legend',
    sectionSettings: 'Settings',
    gradientToggle: 'Colour gradient',
    gradientHelp: 'Smooth colour within classes A and C (stronger the further from average, up to the 1st/99th percentile). The middle (B) stays solid yellow.',
    labelSubject: 'Subject',
    labelMetric: 'Metric',
    labelBaseline: 'Reference point',
    levelNational: 'Whole country',
    levelVoivodeship: 'Voivodeship',
    levelPowiat: 'County',
    levelGmina: 'Municipality',
    labelPublic: 'Public',
    publicAll: 'All',
    publicYes: 'Yes',
    publicNo: 'No',
    labelThreshold: 'Min. score',
    labelMinYears: 'Min. years of data',
    searchPlaceholder: 'e.g. Marszałkowska 1, Warszawa',
    searchButton: 'Search',
    searchHelp: 'Geocoding: OpenStreetMap Nominatim. Searches only on submit.',
    searchNotFound: 'Address not found.',
    searchError: 'Geocoding error.',
    legendGood: 'Above average (> +0.33σ)',
    legendMedium: 'Around average (±0.33σ)',
    legendWeak: 'Below average (< −0.33σ)',
    legendGradientNote: 'Within classes A and C the colour is continuous — the further from average, the stronger, up to the 1st and 99th percentile. That is why the map shows more than three shades. Class B stays a flat yellow.',
    metric_mean: 'Mean',
    metric_median: 'Median',
    metric_diff_mean: 'Difference from mean',
    metric_unit_norm_diff_mean: 'Normalised score',
    subject_polski: 'Polish',
    subject_matematyka: 'Maths',
    subject_angielski: 'English',
    subject_composite_min: 'Weakest subject',
    popupPublic: 'Public',
    popupPrivate: 'Private',
    popupYears: 'years of data',
    popupScore: 'Score',
    popupRank: 'Rank',
    popupPct: 'Percentile',
    helpPct: 'The share of schools whose result is no better than this school\'s. 50 means half did worse or the same; 100 is the best result in the set. Computed among the schools present in the same view.',
    popupComposite: 'Weakest of 3',
    warnShortHistory: 'Short history (< 3 years) — less certain.',
    warnVolatile: 'High year-to-year volatility — score depends on which years are included.',
    rankingTitle: 'School ranking — Mazowieckie',
    rankingNameSearch: 'Search by name or location',
    rankingSearchPlaceholder: 'e.g. STO, Vizja, Słupica',
    lastKRow: (k) => `last ${k}`,
    rankingView: 'View',
    rankingViewParam: 'View parameter',
    rankingViewBase: 'all years (base)',
    rankingViewLastK: 'last k years (last_k)',
    rankingViewSingleYear: 'single year (single_year)',
    rankingViewLOO: 'leave-one-out (LOO)',
    colName: 'School',
    colStreet: 'Street',
    colTown: 'Town',
    colGmina: 'Municipality',
    colPowiat: 'County',
    colPublic: 'Public',
    colNYears: 'Years',
    colRank: 'Rank',
    colLOORange: 'Rank range (LOO)',
    colSingleRange: 'Rank range (single-year)',
    helpLOORange: 'Range of ranks when each year is left out in turn (leave-one-out jackknife). A wide range means the position depends a lot on which year is included.',
    helpSingleRange: 'Range of ranks computed from each single year alone. A wide range means big year-to-year swings.',
    colScore: 'Score',
    colClass: 'Class',
    clickHint: 'Click a row to expand school details.',
    detailClassByYear: (s) => `Class (${s}) by year`,
    detailViewLOO: 'LOO',
    detailViewSingle: 'single years',
    detailShowTables: 'Show numeric tables',
    detailHideTables: 'Hide numeric tables',
    detailSecSingle: 'Single years',
    detailSecLOO: 'LOO (year left out)',
    detailSecLastK: 'Last k years',
    detailSecBase: 'Score across all years (base)',
    detailWeakestNote: 'Bold = the subject with the lowest score (the one that sets composite_min), regardless of the dimension shown (score/rank/percentile).',
    offMap: 'no location',
    rowsShown: (n, total) => `${n} of ${total} schools`,
    regionNoSchools: 'No schools with exam results',
    regionTooSmall: 'Reference group too small to score',
    regionOnlyChild: 'The only unit within its parent — nothing to compare it against',
    schoolsInRegion: (n) => `${n} ${n === 1 ? 'school' : 'schools'}`,
    dataYears: (lo, hi) => `8th-grade exam ${lo}–${hi}`,
    historyLoading: 'Loading detailed data…',
    historyFailed: 'Could not load the year-by-year data — try refreshing.',
    chartYearsCaption: 'Score in each year',
    helpPopupChart: 'The chart plots the score computed from each year on its own — not the multi-year score, and not the LOO version. The multi-year score is in the table above.',
    helpMetric: 'Mean is the plain percentage score — the share of points this school\'s pupils scored on average. Normalised score is the distance from the voivodeship average of the same year: 0 is exactly average, positive values sit above it, negative below. Click to read about all the metrics.',
    helpBaseline: 'A school\'s score is its distance from the mean of a reference group. This choice sets that group: the whole country, the voivodeship, the county or the municipality. The narrower the group, the more the score says "compared with its immediate surroundings" rather than "compared with the country".',
    baselineFollowsZoom: 'Applies to the school view only. Above it a region\'s colour always compares it with its parent unit.',
    baselineRawMetric: 'Mean and Median are raw percentage scores — they have no reference population, so this choice does not change them.',
    advancedMetrics: 'Advanced metrics',
    advancedMetricsHelp: 'Adds "Median" and "Difference from mean". Difference from mean orders schools exactly as the normalised score does — only the scale of the numbers changes.',
    chartDiffCaption: 'LOO vs single years — what is the difference?',
    helpChartDiff: 'Single years: each point uses that one year alone — it shows how much the score swings from year to year. LOO ("leave-one-out"): each point is the score over all years except that one, so the points move less because each still rests on the remaining years. A wide spread of LOO points means the school\'s score depends heavily on a single year.',
    publicYesShort: 'Yes',
    publicNoShort: 'No',
    langPL: 'PL',
    langEN: 'EN',
  },
};

let currentLang = 'pl';

// A "?" carrying its explanation in data-help, shown by the CSS tooltip. Use
// where the bubble has room to open; inside a scroll container prefer
// helpDetailsHTML, which expands in the flow instead of being positioned.
function helpIconHTML(key) {
  const text = String(t(key))
    .replaceAll('&', '&amp;').replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;').replaceAll('"', '&quot;');
  return `<span class="help-icon" tabindex="0" role="button" aria-label="?" data-help="${text}">i</span>`;
}

// Inline help disclosure: a caption plus a "?" affordance that expands its
// explanation in the document flow. Used where an absolutely-positioned tooltip
// would be clipped — the Leaflet popup scrolls (overflow-y:auto) and the ranking
// chart grid scrolls sideways on mobile (overflow-x:auto).
function helpDetailsHTML(captionKey, helpKey) {
  return `<details class="chart-help">
      <summary><span>${t(captionKey)}</span><span class="help-dot" aria-hidden="true">i</span></summary>
      <p>${t(helpKey)}</p>
    </details>`;
}

function t(key, ...args) {
  const v = (I18N[currentLang] && I18N[currentLang][key]) || I18N.pl[key] || key;
  return (typeof v === 'function') ? v(...args) : v;
}

function applyI18N(root = document) {
  for (const el of root.querySelectorAll('[data-i18n]')) {
    el.textContent = t(el.getAttribute('data-i18n'));
  }
  for (const el of root.querySelectorAll('[data-i18n-attr]')) {
    const spec = el.getAttribute('data-i18n-attr');
    const [attr, key] = spec.split('|');
    if (attr && key) el.setAttribute(attr, t(key));
  }
}

function setLang(lang) {
  currentLang = (lang === 'en') ? 'en' : 'pl';
  document.documentElement.lang = currentLang;
  applyI18N();
}

// Wire the #lang-toggle control (present in every page's nav). It is a two-option
// segment "PL | EN" with the ACTIVE language highlighted/bold — unambiguous (a
// single letter reads either as the current state or the action) and it surfaces
// that the other language exists. Clicking the inactive option switches to it,
// re-translates static [data-i18n] labels (via setLang), persists the choice,
// then calls onAfterChange so the page can re-render its dynamic, language-
// dependent content (select options, table, popups) — that part differs per page.
function wireLangToggle(onAfterChange) {
  const el = document.getElementById('lang-toggle');
  if (!el) return;
  const LANGS = ['pl', 'en'];
  const render = () => {
    el.innerHTML = LANGS.map(l =>
      `<button type="button" class="lang-opt${l === currentLang ? ' active' : ''}" `
      + `data-lang="${l}" aria-pressed="${l === currentLang}">${l.toUpperCase()}</button>`
    ).join('');
  };
  render();
  el.addEventListener('click', (e) => {
    const opt = e.target.closest('.lang-opt');
    if (!opt) return;
    const lang = opt.getAttribute('data-lang');
    if (lang === currentLang) return;
    setLang(lang);
    writePref('lang', currentLang);
    render();
    if (onAfterChange) onAfterChange();
  });
}

// Fill the #data-years subtitle (if present) from the loaded scale metadata.
// Range min–max, so it auto-updates when a new exam year is added. Re-callable
// (e.g. after a language switch) since the label text is language-dependent.
function fillDataYears() {
  const el = document.getElementById('data-years');
  if (!el || !scaleData) return;
  const years = scaleData.metadata.years_in_data;
  if (!years || !years.length) return;
  el.textContent = t('dataYears', Math.min(...years), Math.max(...years));
}

// -----------------------------------------------------------------------------
// Helpers used by both pages

function fillMetricSelect(selectEl, currentMetric, advanced) {
  selectEl.innerHTML = '';
  // Keep the selected metric listed even when it is advanced and the toggle is
  // off (a deep link can put us there), so the select never shows a value the
  // user cannot see.
  const listed = visibleMetrics(advanced);
  const options = listed.includes(currentMetric) ? listed : [...listed, currentMetric];
  for (const m of options) {
    const opt = document.createElement('option');
    opt.value = m;
    opt.textContent = t('metric_' + m);
    if (m === currentMetric) { opt.selected = true; opt.setAttribute('selected', ''); }
    selectEl.appendChild(opt);
  }
}

function fillSubjectSelect(selectEl, currentSubject) {
  selectEl.innerHTML = '';
  for (const s of SUBJECTS) {
    const opt = document.createElement('option');
    opt.value = s;
    opt.textContent = t('subject_' + s);
    if (s === currentSubject) { opt.selected = true; opt.setAttribute('selected', ''); }
    selectEl.appendChild(opt);
  }
}

function fmtScore(score, metric) {
  if (score == null || Number.isNaN(score)) return '—';
  if (metric === 'mean' || metric === 'median') return score.toFixed(1);
  if (metric === 'diff_mean') return (score >= 0 ? '+' : '') + score.toFixed(1);
  return (score >= 0 ? '+' : '') + score.toFixed(3);
}

// Same as fmtScore but renders the decimals in a muted/smaller span so the
// ranking table can show 3 decimals without visually shouting them — the
// integer part is the main read, the decimals are for breaking apparent ties.
// Returns HTML; only use in trusted DOM (we control the score values).
function fmtScoreHTML(score, metric) {
  if (score == null || Number.isNaN(score)) return '—';
  let formatted;
  if (metric === 'mean' || metric === 'median') {
    formatted = score.toFixed(3);
  } else {
    formatted = (score >= 0 ? '+' : '') + score.toFixed(3);
  }
  const dotIdx = formatted.indexOf('.');
  if (dotIdx < 0) return formatted;
  const integer = formatted.slice(0, dotIdx);
  const decimal = formatted.slice(dotIdx);
  return `${integer}<span class="dec">${decimal}</span>`;
}

function isPublic(s) { return s.is_public === 'Tak'; }
