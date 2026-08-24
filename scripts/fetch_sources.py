"""Download the pinned CIE source files into data/egzamin-osmoklasisty/.

Run from the repo root:
    uv run python scripts/fetch_sources.py
    uv run python scripts/fetch_sources.py --dry-run

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

from school_quality.sources import MANIFEST_URL, resolve_school_files

USER_AGENT = 'compare-primary-schools/1.0 (+https://github.com/herbakamil)'


def fetch(url: str) -> bytes:
    request = Request(url, headers={'User-Agent': USER_AGENT})
    with urlopen(request, timeout=60) as response:
        return response.read()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=Path('data/egzamin-osmoklasisty'))
    parser.add_argument('--dry-run', action='store_true', help='list what would be fetched')
    parser.add_argument('--force', action='store_true', help='re-download files already present')
    args = parser.parse_args()

    manifest = json.loads(fetch(MANIFEST_URL).decode('utf-8'))
    resolved = resolve_school_files(manifest)
    print(f'{len(resolved)} pinned school-level files')

    for item in resolved:
        target = args.data_dir / item['local_name']
        if target.exists() and not args.force:
            print(f'  have    {item["local_name"]}')
            continue
        if args.dry_run:
            print(f'  would fetch {item["local_name"]}  <- {item["url"]}')
            continue
        payload = fetch(item['url'])
        target.write_bytes(payload)
        print(f'  fetched {item["local_name"]}  ({len(payload):,} bytes)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
