"""Download the pinned CIE source files of one exam into its data directory.

Run from the repo root:
    uv run python scripts/fetch_sources.py
    uv run python scripts/fetch_sources.py --dry-run
    uv run python scripts/fetch_sources.py --exam matura

--exam e8 (the default) writes to data/egzamin-osmoklasisty/ with EDITION_PINS;
--exam matura writes to data/egzamin-maturalny/ with MATURA_EDITION_PINS.

Existing files are left alone unless --force is given: the xlsx are the
project's input data and re-downloading them is how you would accidentally
adopt a revised edition.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from school_quality.sources import EXAM_KEYS, MANIFEST_URL, resolve_school_files

USER_AGENT = 'szkolomierz/1.0 (+https://github.com/mikus/szkolomierz)'

DATA_DIRS = {
    'e8': Path('data/egzamin-osmoklasisty'),
    'matura': Path('data/egzamin-maturalny'),
}


def fetch(url: str) -> bytes:
    request = Request(url, headers={'User-Agent': USER_AGENT})
    with urlopen(request, timeout=60) as response:
        return response.read()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exam', choices=sorted(EXAM_KEYS), default='e8')
    parser.add_argument('--data-dir', type=Path, default=None,
                        help='default: data/egzamin-osmoklasisty (e8), '
                             'data/egzamin-maturalny (matura)')
    parser.add_argument('--dry-run', action='store_true', help='list what would be fetched')
    parser.add_argument('--force', action='store_true', help='re-download files already present')
    args = parser.parse_args()
    data_dir = DATA_DIRS[args.exam] if args.data_dir is None else args.data_dir

    manifest = json.loads(fetch(MANIFEST_URL).decode('utf-8'))
    resolved = resolve_school_files(manifest, exam=EXAM_KEYS[args.exam])
    print(f'{len(resolved)} pinned school-level files')

    for item in resolved:
        target = data_dir / item['local_name']
        if target.exists() and not args.force:
            print(f'  have    {item["local_name"]}')
            continue
        if args.dry_run:
            print(f'  would fetch {item["local_name"]}  <- {item["url"]}')
            continue
        payload = fetch(item['url'])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        print(f'  fetched {item["local_name"]}  ({len(payload):,} bytes)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
