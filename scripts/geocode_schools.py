#!/usr/bin/env python3
"""Geocode schools to latitude/longitude, via RSPO id first, Nominatim as fallback.

This script is meant to be run occasionally — only when new schools appear or
addresses change. Each school's RSPO id is looked up directly against the RSPO
register; only a school with no usable RSPO geotag falls through to Nominatim
address geocoding, which is slower (rate-limited to ~1 request/second) and
depends on the address text. The result is stable, so it is cached in a CSV
that the notebook reads.

Workflow
--------
1. Reads the schools and their addresses from docs/data/schools-index.json
   (which the analysis notebook produces).
2. Reads the existing cache data/school_coords.csv (if present).
3. For each school:
   - if the RSPO already exists in the cache AND its address is unchanged,
     the cached coordinates are kept (and stay in their original CSV position);
   - otherwise (new RSPO, or changed address) the address is geocoded and
     either updated in place (changed address) or appended at the end (new RSPO).
4. Writes the updated cache back to data/school_coords.csv.

Both routes are gated on the school's own voivodeship, taken from the TERYT the
exam data assigns it and tested against the PRG polygon in docs/geo/kraj.json.
A coordinate that lands outside is dropped rather than written: a marker 600 km
from the school is not partial information, it is wrong information carrying the
same confidence as everything else on the map, while a school with no
coordinates says something true and lands in the triage report.

CSV columns: rspo, miejscowosc, ulica_nr, latitude, longitude

Usage
-----
Nominatim requires a contact (email or URL) in the User-Agent. Supply your own
via the NOMINATIM_CONTACT env var (or the --contact flag); it is never stored in
this repo. If no contact is set, the script warns and skips the Nominatim
fallback (those schools are counted as unmapped) — RSPO lookups need no contact
and run regardless.

    NOMINATIM_CONTACT=you@example.com uv run python scripts/geocode_schools.py
    NOMINATIM_CONTACT=you@example.com uv run python scripts/geocode_schools.py --limit 50  # only 50 new (testing)
    NOMINATIM_CONTACT=you@example.com uv run python scripts/geocode_schools.py --force      # re-geocode everything
    uv run python scripts/geocode_schools.py --contact you@example.com                      # contact via flag

Requirements: requests (already a project dependency via jupyter stack, or add it).
"""

from __future__ import annotations

import argparse
import csv
import http.client
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

# ── Paths (relative to project root; script lives in scripts/) ──────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHOOLS_INDEX_JSON = PROJECT_ROOT / 'docs' / 'data' / 'schools-index.json'
COORDS_CSV = PROJECT_ROOT / 'data' / 'school_coords.csv'
UNMAPPED_CSV = PROJECT_ROOT / 'data' / 'school_coords_unmapped.csv'
# The PRG voivodeship polygons the map already ships. Committed, so the accept
# rule below costs no new data and no network call.
VOIVODESHIP_GEOJSON = PROJECT_ROOT / 'docs' / 'geo' / 'kraj.json'

sys.path.insert(0, str(PROJECT_ROOT / 'src'))
from school_quality.geometry import bounding_box, contains_point
from school_quality.rspo import geotag_from_payload, rspo_detail_url

# Threshold for "suspicious shared-coordinate group". With the new strategy
# we expect no shared coords at all — except for genuine cases (a school
# complex at one address). 3+ at the same point is unlikely to be that and
# should be eyeballed.
SHARED_COORD_WARN_THRESHOLD = 3

NOMINATIM_URL = 'https://nominatim.openstreetmap.org/search'
REQUEST_DELAY_SECONDS = 1.1  # Nominatim usage policy: max 1 request/second

# Street prefixes the OKE data tacks on (e.g. "ul. Marszałkowska 1"). Nominatim
# fares better when we either drop them or also try a stripped form.
STREET_PREFIXES = ('ul.', 'Ul.', 'UL.', 'al.', 'Al.', 'AL.', 'pl.', 'Pl.', 'os.', 'Os.')

# Nominatim requires a User-Agent identifying the application AND a way to
# contact whoever runs it (stock HTTP-library User-Agents are blocked). The app
# id lives in source, but the contact must NOT be hardcoded — this repo is
# public. It is supplied at runtime via the NOMINATIM_CONTACT env var (or the
# --contact flag), and slotted into this template. See README "Geocoding".
CONTACT_ENV_VAR = 'NOMINATIM_CONTACT'
USER_AGENT_TEMPLATE = (
    'szkolomierz/1.0 (school quality map; contact: {contact})'
)

CSV_COLUMNS = ['rspo', 'miejscowosc', 'ulica_nr', 'latitude', 'longitude']


def normalize_address(miejscowosc: str | None, ulica_nr: str | None) -> str:
    """Build a normalized address key for comparison (lowercased, stripped).

    The key drops the street-type prefix, because `_strip_street_prefix` already
    drops it before the address is sent to Nominatim: "ul. Kopernika 5" and
    "Kopernika 5" produce the identical query, so treating them as different
    addresses would re-geocode a school for a result that cannot change. The 2026
    OKE file stopped writing the prefix for ~570 schools, which would otherwise
    invalidate their cached coordinates for nothing.
    """
    parts = [str(miejscowosc or '').strip(),
             _strip_street_prefix(str(ulica_nr or '').strip())]
    return '|'.join(p.lower() for p in parts)


def resolve_user_agent(cli_contact: str | None) -> str | None:
    """Build the Nominatim User-Agent from a runtime-supplied contact.

    Contact precedence: --contact flag, then the NOMINATIM_CONTACT env var.
    Returns None and warns if neither is set — RSPO is tried first now, so a
    run that never falls through to Nominatim should not abort for want of a
    contact it will not use. `geocode_address` is skipped when this is None.
    """
    contact = (cli_contact or os.environ.get(CONTACT_ENV_VAR) or '').strip()
    if not contact:
        print(
            f'WARNING: no Nominatim contact set. Schools not resolved via RSPO will '
            f'be skipped by the address geocoder and counted as unmapped.\n'
            f'  Pass it inline:   {CONTACT_ENV_VAR}=you@example.com uv run python scripts/geocode_schools.py\n'
            f'  Or via the flag:  uv run python scripts/geocode_schools.py --contact you@example.com\n'
            f'See the README "Geocoding" section for details.',
            file=sys.stderr,
        )
        return None
    return USER_AGENT_TEMPLATE.format(contact=contact)


def _strip_street_prefix(street: str) -> str:
    """Drop the leading 'ul.'/'al.'/'pl.'/'os.' tag and collapse whitespace."""
    s = street.strip()
    for prefix in STREET_PREFIXES:
        if s.startswith(prefix):
            s = s[len(prefix) :].strip()
            break
    return s


def load_voivodeship_polygons(path: Path) -> dict[str, dict]:
    """PRG voivodeship geometry, keyed by the 2-digit TERYT prefix."""
    features = json.loads(path.read_text(encoding='utf-8'))['features']
    return {f['properties']['JPT_KOD_JE'][:2]: f['geometry'] for f in features}


def _viewbox(geometry: dict) -> str:
    """Nominatim's viewbox: "left,top,right,bottom" (W lon, N lat, E lon, S lat)."""
    lon_min, lat_min, lon_max, lat_max = bounding_box(geometry)
    return f'{lon_min},{lat_max},{lon_max},{lat_min}'


def _inside(geometry: dict, coords: tuple[float, float] | None) -> bool:
    """True if a (lat, lon) pair falls inside `geometry`. None is never inside.

    Note the swap: coordinates travel lat-first through this script and lon-first
    through GeoJSON, and this is the one place the two meet.
    """
    return coords is not None and contains_point(geometry, coords[1], coords[0])


def _nominatim_request(params: dict, user_agent: str) -> list:
    """One Nominatim request. Returns the parsed JSON list (possibly empty)."""
    url = f'{NOMINATIM_URL}?{urlencode(params)}'
    request = Request(url, headers={'User-Agent': user_agent, 'Accept-Language': 'pl'})
    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode('utf-8'))
    # Everything the network and the response can throw at us: URLError, HTTPError
    # and TimeoutError all subclass OSError, and a truncated or non-JSON body
    # raises the other two. http.client.HTTPException (parent of IncompleteRead,
    # raised on a truncated HTTP body) subclasses Exception rather than OSError,
    # so it needs listing separately. A bug in this function (a bad params dict,
    # say) is not in that set and should surface as a traceback rather than
    # quietly becoming one more unmapped school.
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, http.client.HTTPException) as exc:
        print(f'    request failed: {exc}', file=sys.stderr)
        return []
    finally:
        time.sleep(REQUEST_DELAY_SECONDS)


RSPO_DELAY_SECONDS = 0.15  # measured ~0.09s/request; this leaves headroom


def _rspo_geotag(rspo: int) -> tuple[float, float] | None:
    """One RSPO detail lookup. Returns None on any failure - the caller falls
    back to the address geocoder, so a miss must never abort the run."""
    request = Request(rspo_detail_url(rspo), headers={'User-Agent': 'szkolomierz/1.0'})
    try:
        with urlopen(request, timeout=20) as response:
            return geotag_from_payload(json.loads(response.read().decode('utf-8')))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, http.client.HTTPException) as exc:
        print(f'    RSPO lookup failed for {rspo}: {exc}', file=sys.stderr)
        return None
    finally:
        time.sleep(RSPO_DELAY_SECONDS)


def geocode_address(
    miejscowosc: str | None,
    ulica_nr: str | None,
    user_agent: str | None,
    voivodeship: dict,
) -> tuple[float, float] | None:
    """Geocode a single address via Nominatim, bounded to one voivodeship.

    Strategy (try in order, accept the first result inside `voivodeship`):
      1. Structured query: street + city + country=Polska.
      2. Free-text: "<street>, <city>, Polska".
      3. Free-text with the original prefixed street ("ul. X").

    All three are viewbox-bounded to the school's own voivodeship, and every
    candidate is then tested against that voivodeship's polygon. The box alone
    would not do: Poland's voivodeships interlock, so a box around one covers
    large parts of four others — which is how a same-named village on the far
    side of the country used to pass for a match under the old Poland-wide box.
    The polygon is the gate; the viewbox only points Nominatim's ranking at the
    right part of the map. The voivodeship **name** stays out of the query text
    on purpose (see CLAUDE.md): asserting a region in free text degrades
    Nominatim's own ranking rather than helping it.

    There is **no** fallback to a town-only query. If no street-level match is
    found within the voivodeship, return None — the school will appear in the
    ranking but stay off the map. Better than planting it on a city centroid
    (the previous behaviour silently put 773 of 1,720 schools on top of each
    other at the Pałac Kultury location and similar).

    `user_agent` is None when no Nominatim contact was configured; in that
    case this is skipped entirely (returns None) rather than sending
    requests Nominatim would reject.
    """
    if user_agent is None:
        return None
    miejscowosc = (miejscowosc or '').strip()
    ulica_raw = (ulica_nr or '').strip()
    if not miejscowosc or not ulica_raw:
        return None  # no street → no street-level match possible

    street_clean = _strip_street_prefix(ulica_raw)
    bounds = {'countrycodes': 'pl', 'viewbox': _viewbox(voivodeship), 'bounded': '1'}
    queries: list[dict] = []

    # 1. Structured query — Nominatim prefers `street=<housenumber> <streetname>`
    #    or `street=<streetname> <housenumber>`; both forms work in practice.
    queries.append(
        {
            'street': street_clean,
            'city': miejscowosc,
            'country': 'Polska',
            'format': 'json',
            'limit': '1',
            **bounds,
        }
    )

    # 2. Free-text. The region is expressed as a viewbox, never as words.
    queries.append(
        {
            'q': f'{street_clean}, {miejscowosc}, Polska',
            'format': 'json',
            'limit': '1',
            **bounds,
        }
    )

    # 3. Original "ul. X" form — some streets disambiguate better with the tag.
    if street_clean != ulica_raw:
        queries.append(
            {
                'q': f'{ulica_raw}, {miejscowosc}, Polska',
                'format': 'json',
                'limit': '1',
                **bounds,
            }
        )

    for params in queries:
        data = _nominatim_request(params, user_agent)
        if not data:
            continue
        try:
            lat = float(data[0]['lat'])
            lon = float(data[0]['lon'])
        except (KeyError, ValueError, TypeError):
            continue
        if not _inside(voivodeship, (lat, lon)):
            # The chosen result drifted outside the voivodeship even though the
            # query was bounded to it (Nominatim's viewbox is a soft bias, not
            # a hard filter). Reject and try the next strategy.
            continue
        return lat, lon

    return None


def load_existing_cache(path: Path) -> list[dict]:
    """Load existing coordinate cache as an ordered list of row dicts."""
    if not path.exists():
        return []
    with path.open(newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def load_schools(path: Path) -> list[dict]:
    """Load schools (rspo, teryt, miejscowosc, ulica_nr) from schools-index.json.

    The index is columnar — parallel arrays under 'schools', one element per
    school — so the columns this script needs are zipped back into rows.
    strict=True because a length mismatch between them would otherwise truncate
    silently, dropping schools off the end of the shortest column.

    `teryt` is the gmina code the exam data assigns the school; its first two
    digits are the voivodeship both geocoding routes are gated on.
    """
    if not path.exists():
        raise FileNotFoundError(
            f'{path} not found. Run the analysis notebook first to generate it.'
        )
    columns = json.loads(path.read_text(encoding='utf-8'))['schools']
    return [
        {'rspo': rspo, 'teryt': teryt, 'miejscowosc': miejscowosc, 'ulica_nr': ulica_nr}
        for rspo, teryt, miejscowosc, ulica_nr in zip(
            columns['rspo'], columns['teryt'], columns['miejscowosc'], columns['ulica_nr'],
            strict=True)
    ]


def write_cache(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({col: row.get(col, '') for col in CSV_COLUMNS})


def _gmaps_url(miejscowosc: str, ulica_nr: str) -> str:
    """A Google Maps search URL for the school address — clickable in the CSV."""
    from urllib.parse import quote_plus

    parts = [p for p in [ulica_nr, miejscowosc, 'Polska'] if p]
    q = quote_plus(', '.join(parts))
    return f'https://www.google.com/maps/search/?api=1&query={q}'


def _row_has_coords(row: dict | None) -> bool:
    """True if a cache row carries both halves of a coordinate."""
    return bool(row) and bool(row.get('latitude')) and bool(row.get('longitude'))


def write_unmapped_report(schools: list[dict], cache_rows: list[dict], path: Path) -> int:
    """Write a CSV listing every school left without coordinates.

    That is three different situations with one consequence: no street-level
    match was found, the register's geotag was rejected by the voivodeship gate,
    or the school was never attempted at all. All three leave the school off the
    map, and all three want the same manual triage.

    Columns: rspo, miejscowosc, ulica_nr, google_maps_search.
    The Google Maps URL is meant for manual triage: open it, find the school,
    eyeball the coordinates, and paste them into school_coords.csv by hand.

    Driven by the school population, not by the cache. A school with no cache
    row at all — one the index gained after the last run, or one a --limit run
    never reached — has nothing for a cache-driven filter to catch, so it fell
    out of the triage list as well as off the map and there was nowhere left
    pointing at it. Exactly one school (rspo 269571) was in that state. The
    addresses come from the index for the same reason: there may be no cache
    row to read them from.

    Returns the count of unmapped schools (so the caller can summarise).
    """
    cache_by_rspo = {int(row['rspo']): row for row in cache_rows}
    unmapped = [s for s in schools if not _row_has_coords(cache_by_rspo.get(s['rspo']))]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(
            f,
            fieldnames=['rspo', 'miejscowosc', 'ulica_nr', 'google_maps_search'],
        )
        writer.writeheader()
        for school in unmapped:
            miejscowosc = str(school.get('miejscowosc') or '')
            ulica_nr = str(school.get('ulica_nr') or '')
            writer.writerow(
                {
                    'rspo': school['rspo'],
                    'miejscowosc': miejscowosc,
                    'ulica_nr': ulica_nr,
                    'google_maps_search': _gmaps_url(miejscowosc, ulica_nr),
                }
            )
    return len(unmapped)


def report_shared_coords(rows: list[dict], warn_threshold: int) -> list[tuple]:
    """Find groups of schools sharing the same (lat, lon).

    With the no-centroid-fallback rule, large groups should not exist —
    they would imply a leftover centroid match or some other systematic
    error. Small groups (2 schools) can be real (school complex). The
    warn_threshold sets where we flag the issue.

    Returns a list of (count, coord, [rspos]) for groups at or above the
    threshold, sorted by count descending.
    """
    from collections import defaultdict

    by_coord: dict[tuple, list[str]] = defaultdict(list)
    for r in rows:
        lat = r.get('latitude')
        lon = r.get('longitude')
        if lat and lon:
            by_coord[(lat, lon)].append(str(r.get('rspo', '')))

    groups = [
        (len(rspos), coord, rspos)
        for coord, rspos in by_coord.items()
        if len(rspos) >= warn_threshold
    ]
    # Sort by count only: coord/rspos are not comparable across groups where one
    # side came from the CSV cache (str lat/lon) and the other was freshly
    # geocoded this run (float lat/lon) - sorting the full tuple crashes on that
    # mismatch as soon as two groups tie on count.
    groups.sort(key=lambda g: g[0], reverse=True)
    return groups


# ── Refactored pipeline: plan → run → summary → reports ─────────────────────


@dataclass
class GeocodingPlan:
    """Everything `run_geocoding_loop` needs to do its job, decided up front."""

    schools_by_rspo: dict[int, dict]
    cache_by_rspo: dict[int, dict]
    # CSV order — preserved through the run, extended for newly-appended rspos.
    ordered_rspo: list[int]
    # Schools we will actually re-geocode this run.
    to_geocode: list[tuple[int, dict, str]] = field(default_factory=list)
    # Schools dropped by --limit and skipped this run (their cached row falls
    # through unchanged).
    deferred: list[tuple[int, dict, str]] = field(default_factory=list)
    # Rows we won't touch (address unchanged and a coord is on file).
    kept_count: int = 0


@dataclass
class GeocodingResult:
    """Output of `run_geocoding_loop`."""

    rows: list[dict]
    updated_count: int
    new_count: int
    # Schools whose RSPO register geotag was thrown away for falling outside the
    # voivodeship the exam data assigns them. Kept separate from "not found"
    # because it is not the same fact: two authoritative sources disagree about
    # where the school is, and one of them is wrong. See print_rejected_geotags.
    rejected_geotags: list[dict] = field(default_factory=list)


def plan_geocoding(
    schools: list[dict], existing_rows: list[dict], limit: int | None
) -> GeocodingPlan:
    """Decide which schools to re-geocode, which to skip, and which to defer.

    A school is queued when its address changed since the cached row OR the
    cached row has no coordinates. New schools (rspo not in cache) are
    appended. `limit`, if set, caps the queue and moves the rest to deferred.
    """
    cache_by_rspo = {int(row['rspo']): row for row in existing_rows}
    ordered_rspo = [int(row['rspo']) for row in existing_rows]
    schools_by_rspo = {s['rspo']: s for s in schools}

    plan = GeocodingPlan(
        schools_by_rspo=schools_by_rspo,
        cache_by_rspo=cache_by_rspo,
        ordered_rspo=list(ordered_rspo),  # the loop may extend this; keep a copy
    )

    # Rows currently in cache: keep if address unchanged + coords present,
    # otherwise queue for re-geocoding.
    queue: list[tuple[int, dict, str]] = []
    for rspo in ordered_rspo:
        cached = cache_by_rspo[rspo]
        school = schools_by_rspo.get(rspo)
        if school is None:
            continue  # cached row for a school that's no longer in the source
        old_addr = normalize_address(cached.get('miejscowosc'), cached.get('ulica_nr'))
        new_addr = normalize_address(school['miejscowosc'], school['ulica_nr'])
        has_coords = bool(cached.get('latitude')) and bool(cached.get('longitude'))
        if old_addr == new_addr and has_coords:
            plan.kept_count += 1
        else:
            queue.append((rspo, school, 'update'))

    # New schools (rspo not in cache) get appended at the end.
    for school in schools:
        if school['rspo'] not in cache_by_rspo:
            queue.append((school['rspo'], school, 'new'))

    if limit is None or limit >= len(queue):
        plan.to_geocode = queue
        plan.deferred = []
    else:
        plan.to_geocode = queue[:limit]
        plan.deferred = queue[limit:]

    return plan


def _assemble_rows(result_by_rspo: dict[int, dict], ordered_rspo: list[int]) -> list[dict]:
    """Lay out the final CSV in `ordered_rspo` order; tail any extras."""
    rows = [result_by_rspo[r] for r in ordered_rspo if r in result_by_rspo]
    ordered_set = set(ordered_rspo)
    for r, row in result_by_rspo.items():
        if r not in ordered_set:
            rows.append(row)
    return rows


def run_geocoding_loop(
    plan: GeocodingPlan,
    user_agent: str | None,
    save_path: Path,
    voivodeships: dict[str, dict],
    save_every: int = 50,
) -> GeocodingResult:
    """Geocode every row in `plan.to_geocode`, saving the CSV periodically.

    Rows in `plan.cache_by_rspo` that aren't being re-geocoded pass through
    unchanged. Deferred rows are also left untouched.

    `voivodeships` maps a 2-digit TERYT prefix to its PRG polygon. Every
    coordinate written here — register geotag or geocoded address alike — has
    been tested against the school's own one.
    """
    # Seed result with every row we are NOT re-geocoding (kept + deferred).
    to_geocode_rspos = {rspo for rspo, _, _ in plan.to_geocode}
    result_by_rspo: dict[int, dict] = {}
    for rspo in plan.ordered_rspo:
        if rspo not in to_geocode_rspos:
            result_by_rspo[rspo] = plan.cache_by_rspo[rspo]

    n_total = len(plan.to_geocode)
    print(
        f'\nGeocoding {n_total:,} schools'
        + (f' ({len(plan.deferred):,} deferred due to --limit)' if plan.deferred else '')
    )

    updated_count = 0
    new_count = 0
    rejected_geotags: list[dict] = []
    ordered_rspo = list(plan.ordered_rspo)  # may grow when new schools land

    for index, (rspo, school, action) in enumerate(plan.to_geocode, start=1):
        pct = index / n_total * 100 if n_total else 100.0
        label = 're-geocoding' if action == 'update' else 'geocoding NEW'
        print(
            f'  [{index:>4,}/{n_total:,} ({pct:5.1f}%)] {label} rspo={rspo}: '
            f'{school["miejscowosc"]}, {school["ulica_nr"]}'
        )
        code = str(school['teryt'])[:2]
        voivodeship = voivodeships.get(code)
        if voivodeship is None:
            raise KeyError(
                f'rspo={rspo}: no voivodeship polygon for TERYT prefix {code!r}. '
                f'{VOIVODESHIP_GEOJSON.name} and schools-index.json disagree; '
                f'refusing to geocode against an unknown region.'
            )
        coords = _rspo_geotag(school['rspo'])
        if coords is not None and not _inside(voivodeship, coords):
            # The register's own geotag puts the school in a different
            # voivodeship than the exam data does. Both are authoritative and
            # one is wrong, so the honest answer is "we do not know where this
            # is" — dropping it here sends the school to the triage report
            # instead of onto the map at a place it demonstrably is not.
            print(f'    RSPO geotag {coords} is outside voivodeship {code} — rejected')
            rejected_geotags.append(
                {'rspo': rspo, 'teryt': school['teryt'], 'miejscowosc': school['miejscowosc'],
                 'ulica_nr': school['ulica_nr'], 'latitude': coords[0], 'longitude': coords[1]}
            )
            coords = None
        if coords is None:
            coords = geocode_address(
                school['miejscowosc'], school['ulica_nr'], user_agent, voivodeship
            )
        if action == 'update':
            updated_count += 1
        else:
            new_count += 1
            ordered_rspo.append(rspo)
        result_by_rspo[rspo] = {
            'rspo': rspo,
            'miejscowosc': school['miejscowosc'],
            'ulica_nr': school['ulica_nr'],
            'latitude': coords[0] if coords else '',
            'longitude': coords[1] if coords else '',
        }

        if index % save_every == 0 and index < n_total:
            write_cache(save_path, _assemble_rows(result_by_rspo, ordered_rspo))
            print(f'    [partial cache saved — {index:,}/{n_total:,} done]')

    final_rows = _assemble_rows(result_by_rspo, ordered_rspo)
    write_cache(save_path, final_rows)
    return GeocodingResult(
        rows=final_rows,
        updated_count=updated_count,
        new_count=new_count,
        rejected_geotags=rejected_geotags,
    )


def print_run_summary(plan: GeocodingPlan, result: GeocodingResult, csv_path: Path) -> None:
    missing = sum(1 for r in result.rows if not r.get('latitude'))
    print()
    print(f'Done. Cache written to {csv_path}')
    print(f'  kept (unchanged):   {plan.kept_count:,}')
    print(f'  updated (changed):  {result.updated_count:,}')
    print(f'  new (appended):     {result.new_count:,}')
    print(f'  total rows:         {len(result.rows):,}')
    print(f'  still missing coords: {missing:,}')


def print_rejected_geotags(rejected: list[dict]) -> None:
    """Name every school whose register geotag disagreed with its own TERYT.

    Deliberately loud and deliberately separate from the unmapped count. These
    are not addresses the geocoder failed to find: they are schools where the
    RSPO register and the OKE exam file place the school in different
    voivodeships. That is a data-quality question about two upstream sources,
    worth an investigation of its own, and it should not disappear into a filter.
    """
    if not rejected:
        print('\n✓ No RSPO geotag fell outside its school\'s own voivodeship.')
        return
    print(f'\n⚠ {len(rejected):,} RSPO geotag(s) rejected: the register places the school in a')
    print('  different voivodeship than the exam data does. Dropped rather than mapped;')
    print('  each one is now in the unmapped triage report. Worth investigating which')
    print('  source is wrong — this is not a geocoding failure.')
    for row in rejected[:20]:
        print(f'    rspo={row["rspo"]} teryt={row["teryt"]} '
              f'({row["miejscowosc"]}, {row["ulica_nr"]}) '
              f'register said {row["latitude"]},{row["longitude"]}')
    if len(rejected) > 20:
        print(f'    … and {len(rejected) - 20:,} more')


def emit_post_run_reports(
    schools: list[dict], rows: list[dict], unmapped_path: Path, warn_threshold: int
) -> None:
    """Refresh the unmapped CSV and print the shared-coords warning.

    Takes both populations: the unmapped report is about schools that should be
    on the map, the shared-coords warning about coordinates actually written.
    """
    n_unmapped = write_unmapped_report(schools, rows, unmapped_path)
    print()
    if n_unmapped:
        print(f'⚠ {n_unmapped:,} school(s) without coordinates — wrote {unmapped_path}')
        print('  Each row in that file has a Google Maps search URL. Open it,')
        print('  find the school, copy the lat/lon, paste into the cache by hand.')
    else:
        print(f'✓ All schools mapped. {unmapped_path.name} is empty.')

    shared = report_shared_coords(rows, warn_threshold)
    print()
    if shared:
        print(f'⚠ {len(shared)} group(s) of ≥{warn_threshold} schools at identical coordinates:')
        for count, coord, rspos in shared[:10]:
            preview = ', '.join(rspos[:5]) + (f', … (+{len(rspos) - 5})' if len(rspos) > 5 else '')
            print(f'    {count:>3} schools at {coord}: rspo={preview}')
        if len(shared) > 10:
            print(f'    … and {len(shared) - 10} more groups')
        print('  Either these are genuine multi-school complexes, or the geocoder')
        print('  is finding the same node for differing addresses — eyeball them.')
    else:
        print(f'✓ No group of ≥{warn_threshold} schools at identical coordinates.')


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--limit',
        type=int,
        default=None,
        help='Maximum number of NEW geocoding requests (for testing).',
    )
    parser.add_argument(
        '--force', action='store_true', help='Re-geocode every school, ignoring the cache.'
    )
    parser.add_argument(
        '--contact',
        default=None,
        help=f'Contact (email or URL) for the Nominatim User-Agent. '
        f'Overrides the {CONTACT_ENV_VAR} env var.',
    )
    parser.add_argument(
        '--report-only',
        action='store_true',
        help='Skip geocoding entirely. Re-emit the unmapped CSV '
        'and shared-coords report from the current cache.',
    )
    return parser.parse_args(argv)


def main() -> None:
    args = _parse_args()

    if args.report_only:
        emit_post_run_reports(
            load_schools(SCHOOLS_INDEX_JSON),
            load_existing_cache(COORDS_CSV),
            UNMAPPED_CSV,
            SHARED_COORD_WARN_THRESHOLD,
        )
        return

    user_agent = resolve_user_agent(args.contact)

    schools = load_schools(SCHOOLS_INDEX_JSON)
    print(f'Loaded {len(schools):,} schools from {SCHOOLS_INDEX_JSON.name}')

    existing_rows = [] if args.force else load_existing_cache(COORDS_CSV)
    print(
        f'Existing cache: {len(existing_rows):,} rows'
        + (' (ignored due to --force)' if args.force else '')
    )

    voivodeships = load_voivodeship_polygons(VOIVODESHIP_GEOJSON)
    print(f'Loaded {len(voivodeships)} voivodeship polygons from {VOIVODESHIP_GEOJSON.name}')

    plan = plan_geocoding(schools, existing_rows, args.limit)
    result = run_geocoding_loop(plan, user_agent, COORDS_CSV, voivodeships)
    print_run_summary(plan, result, COORDS_CSV)
    print_rejected_geotags(result.rejected_geotags)
    emit_post_run_reports(schools, result.rows, UNMAPPED_CSV, SHARED_COORD_WARN_THRESHOLD)


if __name__ == '__main__':
    main()
