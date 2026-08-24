"""Resolve the CIE manifest to the source files this project pins.

CIE publishes school-level results at mapa.wyniki.edu.pl. The asset paths live
under an Angular app's build tree, not an API, so filenames are derived from the
manifest at run time rather than hardcoded.

Most years have two editions and they are NOT equivalent: the September file
revises scores (up to 3.6 points on a school) without meaningfully changing
student counts. Picking by 'newest' would silently change numbers the map
already publishes, so each year pins the edition that reproduces today's output.
Revisiting those pins is a deliberate data decision - see the spec, section 10.
"""

MANIFEST_URL = 'https://mapa.wyniki.edu.pl/MapaEgzaminow/assets/metadata/metadata.json'
ASSET_BASE = 'https://mapa.wyniki.edu.pl/MapaEgzaminow/assets/data/CSV'

# year -> month part of the manifest's `date` field.
EDITION_PINS = {2021: '07', 2022: '09', 2023: '06', 2024: '07', 2025: '07', 2026: '07'}


def remote_filename(year: int, month: str) -> str:
    return f'E8_{year}_szkoly_{month}.xlsx'


def local_filename(year: int, month: str) -> str:
    """Prefix with the year: the notebook loader keys on a leading 4-digit year."""
    return f'{year} - {remote_filename(year, month)}'


def resolve_school_files(manifest: dict, pins: dict | None = None) -> list[dict]:
    """The pinned school-level file for each year, oldest first.

    Years absent from `pins` are skipped, as are non-school granularities.
    Raises ValueError if a pinned edition is not in the manifest - better to stop
    than to quietly fall back to a different edition.
    """
    pins = EDITION_PINS if pins is None else pins
    downloads = manifest['downloads']['E8']
    resolved = []
    for year, month in sorted(pins.items()):
        entries = downloads.get(str(year), [])
        months = {e['date'].split('.')[0] for e in entries if e['code'] == 'szkoly'}
        if not months:
            continue
        if month not in months:
            raise ValueError(
                f'{year}: pinned edition {month!r} is not published. '
                f'Available school-level editions: {sorted(months)}.'
            )
        resolved.append({
            'year': year,
            'month': month,
            'remote_name': remote_filename(year, month),
            'local_name': local_filename(year, month),
            'url': f'{ASSET_BASE}/E8/{year}/{remote_filename(year, month)}',
        })
    return resolved
