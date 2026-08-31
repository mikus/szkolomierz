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
1. Reads the schools and their addresses from docs/data/schools-base.json
   (which the analysis notebook produces).
2. Reads the existing cache data/school_coords.csv (if present).
3. For each school:
   - if the RSPO already exists in the cache AND its address is unchanged,
     the cached coordinates are kept (and stay in their original CSV position);
   - otherwise (new RSPO, or changed address) the address is geocoded and
     either updated in place (changed address) or appended at the end (new RSPO).
4. Writes the updated cache back to data/school_coords.csv.

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
SCHOOLS_BASE_JSON = PROJECT_ROOT / 'docs' / 'data' / 'schools-base.json'
COORDS_CSV = PROJECT_ROOT / 'data' / 'school_coords.csv'
UNMAPPED_CSV = PROJECT_ROOT / 'data' / 'school_coords_unmapped.csv'

sys.path.insert(0, str(PROJECT_ROOT / 'src'))
from school_quality.rspo import geotag_from_payload, rspo_detail_url

# Threshold for "suspicious shared-coordinate group". With the new strategy
# we expect no shared coords at all — except for genuine cases (a school
# complex at one address). 3+ at the same point is unlikely to be that and
# should be eyeballed.
SHARED_COORD_WARN_THRESHOLD = 3

NOMINATIM_URL = 'https://nominatim.openstreetmap.org/search'
REQUEST_DELAY_SECONDS = 1.1  # Nominatim usage policy: max 1 request/second

# Poland bounding box (lon_min, lat_min, lon_max, lat_max), generously rounded.
POLAND_LON_MIN, POLAND_LAT_MIN = 14.0, 48.9
POLAND_LON_MAX, POLAND_LAT_MAX = 24.3, 55.0
# Nominatim viewbox format: "left,top,right,bottom" (west_lon,north_lat,east_lon,south_lat).
POLAND_VIEWBOX = f'{POLAND_LON_MIN},{POLAND_LAT_MAX},{POLAND_LON_MAX},{POLAND_LAT_MIN}'

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
    'compare-primary-schools-mazowieckie/1.0 (school quality map; contact: {contact})'
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


def _in_poland(lat: float, lon: float) -> bool:
    return POLAND_LAT_MIN <= lat <= POLAND_LAT_MAX and POLAND_LON_MIN <= lon <= POLAND_LON_MAX


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
    request = Request(rspo_detail_url(rspo), headers={'User-Agent': 'compare-primary-schools/1.0'})
    try:
        with urlopen(request, timeout=20) as response:
            return geotag_from_payload(json.loads(response.read().decode('utf-8')))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, http.client.HTTPException) as exc:
        print(f'    RSPO lookup failed for {rspo}: {exc}', file=sys.stderr)
        return None
    finally:
        time.sleep(RSPO_DELAY_SECONDS)


def geocode_address(
    miejscowosc: str | None, ulica_nr: str | None, user_agent: str | None
) -> tuple[float, float] | None:
    """Geocode a single address via Nominatim, bounded to Poland.

    Strategy (try in order, accept first result that lands inside Poland):
      1. Structured query: street + city + country=Polska.
      2. Free-text with viewbox-bounded Poland: "<street>, <city>, Polska".
      3. Free-text with original prefixed street ("ul. X"), still viewbox-bounded.

    There is **no** fallback to a town-only query. If no street-level match is
    found within Poland, return None — the school will appear in the
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
    queries: list[dict] = []

    # 1. Structured query — Nominatim prefers `street=<housenumber> <streetname>`
    #    or `street=<streetname> <housenumber>`; both forms work in practice.
    queries.append(
        {
            'street': street_clean,
            'city': miejscowosc,
            'country': 'Polska',
            'countrycodes': 'pl',
            'format': 'json',
            'limit': '1',
        }
    )

    # 2. Free-text, viewbox-bounded to Poland. No region name in the query text:
    #    the school may be in any of the 16 voivodeships, and asserting one it
    #    isn't in would degrade Nominatim's text ranking rather than help it.
    #    countrycodes=pl + POLAND_VIEWBOX (below) do the actual restricting.
    queries.append(
        {
            'q': f'{street_clean}, {miejscowosc}, Polska',
            'format': 'json',
            'limit': '1',
            'countrycodes': 'pl',
            'viewbox': POLAND_VIEWBOX,
            'bounded': '1',
        }
    )

    # 3. Original "ul. X" form — some streets disambiguate better with the tag.
    if street_clean != ulica_raw:
        queries.append(
            {
                'q': f'{ulica_raw}, {miejscowosc}, Polska',
                'format': 'json',
                'limit': '1',
                'countrycodes': 'pl',
                'viewbox': POLAND_VIEWBOX,
                'bounded': '1',
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
        if not _in_poland(lat, lon):
            # The chosen result drifted outside the Poland bbox even though the
            # query was bounded to it (Nominatim's viewbox is a soft bias, not
            # a hard filter). Reject and try next strategy.
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
    """Load schools (rspo, miejscowosc, ulica_nr) from schools-base.json."""
    if not path.exists():
        raise FileNotFoundError(
            f'{path} not found. Run the analysis notebook first to generate it.'
        )
    payload = json.loads(path.read_text(encoding='utf-8'))
    return [
        {
            'rspo': school['rspo'],
            'miejscowosc': school.get('miejscowosc'),
            'ulica_nr': school.get('ulica_nr'),
        }
        for school in payload['schools']
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


def write_unmapped_report(rows: list[dict], path: Path) -> int:
    """Write a CSV listing every school the geocoder couldn't pin to a street.

    Columns: rspo, miejscowosc, ulica_nr, google_maps_search.
    The Google Maps URL is meant for manual triage: open it, find the school,
    eyeball the coordinates, and paste them into school_coords.csv by hand.

    Returns the count of unmapped schools (so the caller can summarise).
    """
    unmapped = [r for r in rows if not (r.get('latitude') and r.get('longitude'))]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(
            f,
            fieldnames=['rspo', 'miejscowosc', 'ulica_nr', 'google_maps_search'],
        )
        writer.writeheader()
        for r in unmapped:
            writer.writerow(
                {
                    'rspo': r.get('rspo', ''),
                    'miejscowosc': r.get('miejscowosc', ''),
                    'ulica_nr': r.get('ulica_nr', ''),
                    'google_maps_search': _gmaps_url(
                        str(r.get('miejscowosc', '')),
                        str(r.get('ulica_nr', '')),
                    ),
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
    plan: GeocodingPlan, user_agent: str | None, save_path: Path, save_every: int = 50
) -> GeocodingResult:
    """Geocode every row in `plan.to_geocode`, saving the CSV periodically.

    Rows in `plan.cache_by_rspo` that aren't being re-geocoded pass through
    unchanged. Deferred rows are also left untouched.
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
    ordered_rspo = list(plan.ordered_rspo)  # may grow when new schools land

    for index, (rspo, school, action) in enumerate(plan.to_geocode, start=1):
        pct = index / n_total * 100 if n_total else 100.0
        label = 're-geocoding' if action == 'update' else 'geocoding NEW'
        print(
            f'  [{index:>4,}/{n_total:,} ({pct:5.1f}%)] {label} rspo={rspo}: '
            f'{school["miejscowosc"]}, {school["ulica_nr"]}'
        )
        coords = _rspo_geotag(school['rspo'])
        if coords is None:
            coords = geocode_address(school['miejscowosc'], school['ulica_nr'], user_agent)
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
    return GeocodingResult(rows=final_rows, updated_count=updated_count, new_count=new_count)


def print_run_summary(plan: GeocodingPlan, result: GeocodingResult, csv_path: Path) -> None:
    missing = sum(1 for r in result.rows if not r.get('latitude'))
    print()
    print(f'Done. Cache written to {csv_path}')
    print(f'  kept (unchanged):   {plan.kept_count:,}')
    print(f'  updated (changed):  {result.updated_count:,}')
    print(f'  new (appended):     {result.new_count:,}')
    print(f'  total rows:         {len(result.rows):,}')
    print(f'  still missing coords: {missing:,}')


def emit_post_run_reports(rows: list[dict], unmapped_path: Path, warn_threshold: int) -> None:
    """Refresh the unmapped CSV and print the shared-coords warning."""
    n_unmapped = write_unmapped_report(rows, unmapped_path)
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
            load_existing_cache(COORDS_CSV),
            UNMAPPED_CSV,
            SHARED_COORD_WARN_THRESHOLD,
        )
        return

    user_agent = resolve_user_agent(args.contact)

    schools = load_schools(SCHOOLS_BASE_JSON)
    print(f'Loaded {len(schools):,} schools from {SCHOOLS_BASE_JSON.name}')

    existing_rows = [] if args.force else load_existing_cache(COORDS_CSV)
    print(
        f'Existing cache: {len(existing_rows):,} rows'
        + (' (ignored due to --force)' if args.force else '')
    )

    plan = plan_geocoding(schools, existing_rows, args.limit)
    result = run_geocoding_loop(plan, user_agent, COORDS_CSV)
    print_run_summary(plan, result, COORDS_CSV)
    emit_post_run_reports(result.rows, UNMAPPED_CSV, SHARED_COORD_WARN_THRESHOLD)


if __name__ == '__main__':
    main()
