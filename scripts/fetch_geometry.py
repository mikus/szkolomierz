"""Download the PRG boundary polygons the map draws, and write them under docs/geo/.

Run from the repo root, once. Boundaries change a few times a decade, so the
result is committed rather than fetched at run time - which also removes a
runtime dependency on a third-party host.

    uv run python scripts/fetch_geometry.py
    uv run python scripts/fetch_geometry.py --dry-run

The TERYT edition matters: gminas are created and merged on 1 January, so which
polygons exist depends on the vintage. Record the vintage in SOURCES.csv the way
the exam editions are recorded.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.request import Request, urlopen

BASE = 'https://mapa.wyniki.edu.pl/MapaEgzaminow/assets/geo/2025'
USER_AGENT = 'compare-primary-schools/1.0 (+https://github.com/herbakamil)'


def fetch(url: str) -> dict:
    request = Request(url, headers={'User-Agent': USER_AGENT})
    with urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode('utf-8'))


def keys_of(collection: dict) -> list[str]:
    return [f['properties']['JPT_KOD_JE'] for f in collection['features']]


def write(path: Path, payload: dict, dry_run: bool) -> None:
    if dry_run:
        print(f'  would write {path} ({len(payload["features"])} features)')
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, separators=(',', ':')),
                    encoding='utf-8')
    print(f'  wrote {path} ({len(payload["features"])} features)')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--geo-dir', type=Path, default=Path('docs/geo'))
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    kraj = fetch(f'{BASE}/kraj/kraj.json')
    write(args.geo_dir / 'kraj.json', kraj, args.dry_run)

    voivodeships = keys_of(kraj)
    print(f'{len(voivodeships)} voivodeships')
    powiats: list[str] = []
    for woj in sorted(voivodeships):
        collection = fetch(f'{BASE}/wojewodztwo/wojewodztwo_{woj}.json')
        write(args.geo_dir / 'woj' / f'{woj}.json', collection, args.dry_run)
        powiats.extend(keys_of(collection))

    print(f'{len(powiats)} powiats')
    for powiat in sorted(powiats):
        collection = fetch(f'{BASE}/powiat/powiat_{powiat}.json')
        write(args.geo_dir / 'pow' / f'{powiat}.json', collection, args.dry_run)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
