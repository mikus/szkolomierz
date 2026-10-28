"""Resolve the CIE manifest to the source files this project pins.

CIE publishes school-level results at mapa.wyniki.edu.pl. The asset paths live
under an Angular app's build tree, not an API, so filenames are derived from the
manifest at run time rather than hardcoded.

Most years have two editions and they are NOT equivalent: the September file
revises scores (up to 3.6 points on a school) without meaningfully changing
student counts. Picking by 'newest' would silently change numbers the map
already publishes, so each year pins the edition that reproduces today's output.
Revisiting those pins is a deliberate data decision - see the spec, section 10.

Two exams are resolved the same way: the 8th-grade exam (manifest key `E8`) and
the new-formula matura (`EM2023`). The manifest key is also the asset path
segment and the filename prefix.
"""

MANIFEST_URL = 'https://mapa.wyniki.edu.pl/MapaEgzaminow/assets/metadata/metadata.json'
ASSET_BASE = 'https://mapa.wyniki.edu.pl/MapaEgzaminow/assets/data/CSV'

# year -> month part of the manifest's `date` field.
EDITION_PINS = {2021: '09', 2022: '09', 2023: '09', 2024: '09', 2025: '09', 2026: '07'}

# September for every year: it revises July's means, and its pass rate counts the
# August resit. Evidence per file in data/egzamin-maturalny/SOURCES.csv.
MATURA_EDITION_PINS = {2023: '09', 2024: '09', 2025: '09', 2026: '09'}

# CLI exam name -> manifest key.
EXAM_KEYS = {'e8': 'E8', 'matura': 'EM2023'}

_DEFAULT_PINS = {'E8': EDITION_PINS, 'EM2023': MATURA_EDITION_PINS}


def remote_filename(year: int, month: str, exam: str = 'E8') -> str:
    return f'{exam}_{year}_szkoly_{month}.xlsx'


def local_filename(year: int, month: str, exam: str = 'E8') -> str:
    """Prefix with the year: the notebook loader keys on a leading 4-digit year."""
    return f'{year} - {remote_filename(year, month, exam)}'


def resolve_school_files(manifest: dict, pins: dict | None = None,
                         exam: str = 'E8') -> list[dict]:
    """The pinned school-level file of `exam` (a manifest key) for each year, oldest first.

    `pins` defaults to the exam's own pins. Years absent from `pins` are skipped.
    Raises ValueError if a pinned edition is not in the manifest, or if a pinned
    year has no school-level entry at all - better to stop than to quietly fall
    back to a different edition (or to none).
    """
    pins = _DEFAULT_PINS[exam] if pins is None else pins
    downloads = manifest['downloads'][exam]
    resolved = []
    for year, month in sorted(pins.items()):
        entries = downloads.get(str(year), [])
        months = {e['date'].split('.')[0] for e in entries if e['code'] == 'szkoly'}
        if not months:
            raise ValueError(f'{year}: no school-level ("szkoly") entry in the manifest.')
        if month not in months:
            raise ValueError(
                f'{year}: pinned edition {month!r} is not published. '
                f'Available school-level editions: {sorted(months)}.'
            )
        resolved.append({
            'year': year,
            'month': month,
            'remote_name': remote_filename(year, month, exam),
            'local_name': local_filename(year, month, exam),
            'url': f'{ASSET_BASE}/{exam}/{year}/{remote_filename(year, month, exam)}',
        })
    return resolved
