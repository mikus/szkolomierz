"""File I/O shared by the export notebooks.

`school_quality` holds pure logic only (its __init__ says so); reading and
writing files lives here, imported by both notebooks so the two exports skip,
write and compare files the same way (matura spec §8.4).
"""

import json
import zipfile
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pandas as pd

# Whitespace is not free at this scale. Pretty-printing the shards with
# indent=1 spent nearly half of a gigabyte on indentation and newlines, which
# is enough on its own to put docs/ over GitHub Pages' 1 GB published-site
# limit. The defaults (', ' / ': ') would still cost two bytes per value.
COMPACT_JSON = (',', ':')


def dumps_json(payload) -> str:
    """Compact JSON with Polish letters kept literal.

    allow_nan=False makes a NaN fail here, at export, instead of in the browser:
    json.dumps would otherwise write a bare NaN, which JSON.parse rejects. Where
    there is no NaN the bytes are exactly json.dumps's.
    """
    return json.dumps(payload, ensure_ascii=False, separators=COMPACT_JSON, allow_nan=False)


def write_json(path: Path, payload) -> None:
    path.write_text(dumps_json(payload), encoding='utf-8')


def json_payloads_equal(new_payload: dict, path: Path) -> bool:
    """Compare new payload with existing JSON file, ignoring metadata.generated_at."""
    if not path.exists():
        return False
    try:
        old_payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return False

    def strip_timestamp(p):
        p = dict(p)
        if 'metadata' in p and isinstance(p['metadata'], dict):
            md = dict(p['metadata'])
            md.pop('generated_at', None)
            p['metadata'] = md
        return p

    return strip_timestamp(new_payload) == strip_timestamp(old_payload)


def dataframes_equal(df_new: pd.DataFrame, df_old: pd.DataFrame,
                     rtol: float = 1e-6, verbose: bool = True) -> bool:
    """Return True if two DataFrames are equal (floats compared with np.isclose).

    When verbose=True, prints what differs.
    """
    if df_new.shape != df_old.shape:
        if verbose:
            print(f'  shape differs: new={df_new.shape}, old={df_old.shape}')
        return False
    if list(df_new.columns) != list(df_old.columns):
        if verbose:
            new_cols = set(df_new.columns)
            old_cols = set(df_old.columns)
            print(f'  columns differ: only_new={new_cols - old_cols}, only_old={old_cols - new_cols}')
        return False

    df_new_reset = df_new.reset_index(drop=True)
    df_old_reset = df_old.reset_index(drop=True)
    for col in df_new_reset.columns:
        a, b = df_new_reset[col], df_old_reset[col]
        if a.dtype.kind == 'f' or b.dtype.kind == 'f':
            if not np.allclose(a.fillna(0), b.fillna(0), rtol=rtol, equal_nan=False) or \
               not (a.isna() == b.isna()).all():
                if verbose:
                    diff_mask = ~np.isclose(a.fillna(0), b.fillna(0), rtol=rtol) | (a.isna() != b.isna())
                    print(f'  column "{col}" differs in {diff_mask.sum()} rows; '
                          f'first diff at row {diff_mask.idxmax()}: '
                          f'new={a[diff_mask.idxmax()]}, old={b[diff_mask.idxmax()]}')
                return False
        else:
            if not a.equals(b):
                if verbose:
                    diff_mask = a.fillna('') != b.fillna('')
                    print(f'  column "{col}" differs in {diff_mask.sum()} rows; '
                          f'first diff at row {diff_mask.idxmax()}: '
                          f'new={a[diff_mask.idxmax()]!r}, old={b[diff_mask.idxmax()]!r}')
                return False
    return True


def commit_export(path: Path, *, unchanged: bool, write: Callable[[], None],
                  force_regenerate: bool, write_output_files: bool, detail: str = '') -> str:
    """Standard skip/write/suppress decision for one export file.

    unchanged: result of the caller's own equality check against the existing file.
    write:     zero-arg callback that performs the actual write.
    detail:    optional text appended to the 'wrote' message (e.g. a row count).

    The two run flags are required keyword arguments with no defaults, so a
    notebook's FORCE_REGENERATE / WRITE_OUTPUT_FILES always reach this decision -
    a frozen copy with WRITE_OUTPUT_FILES = False cannot overwrite live data, and
    a forgotten argument raises TypeError instead of writing.
    Returns 'skipped' | 'wrote' | 'suppressed'.
    """
    if not force_regenerate and unchanged:
        print(f'  {path.name}: no changes, skipping')
        return 'skipped'
    if not write_output_files:
        print(f'  {path.name}: changed but WRITE_OUTPUT_FILES=False — not writing')
        return 'suppressed'
    write()
    suffix = f', {detail}' if detail else ''
    print(f'  {path.name}: wrote {path.stat().st_size / 1024:.1f} KB{suffix}')
    return 'wrote'


def write_xlsx(df: pd.DataFrame, legend_df: pd.DataFrame, path: Path) -> None:
    """Write df to xlsx with a 'data' sheet and a 'legend' sheet.

    view_param is forced to text format so values like '2' don't become 2.0
    when Excel auto-converts.
    """
    with pd.ExcelWriter(path, engine='xlsxwriter') as writer:
        df.to_excel(writer, sheet_name='data', index=False)
        workbook = writer.book
        worksheet = writer.sheets['data']
        worksheet.freeze_panes(1, 0)
        worksheet.autofilter(0, 0, len(df), len(df.columns) - 1)

        # Force view_param to text format (Excel would otherwise treat '2' as a number)
        text_format = workbook.add_format({'num_format': '@'})
        view_param_col_idx = df.columns.get_loc('view_param')
        worksheet.set_column(view_param_col_idx, view_param_col_idx, None, text_format)
        for row_idx, value in enumerate(df['view_param'], start=1):
            worksheet.write_string(row_idx, view_param_col_idx, str(value))

        # Legend sheet
        legend_df.to_excel(writer, sheet_name='legend', index=False)
        legend_ws = writer.sheets['legend']
        legend_ws.set_column(0, 0, 22)
        legend_ws.set_column(1, 1, 90)


def xlsx_unchanged(path: Path, df: pd.DataFrame, legend_df: pd.DataFrame) -> bool:
    """Whether the xlsx at `path` already holds `df` (sheet 'data') and `legend_df` ('legend').

    It reads back both sheets that write_xlsx writes, because they must change
    together: comparing `data` alone meant a legend edit never reached a
    published file (fixed in 3f2357a). view_param is read as text, as written.
    Any unreadable file counts as changed, so the caller rewrites it.
    """
    if not path.exists():
        return False
    try:
        old_df = pd.read_excel(path, sheet_name='data', dtype={'view_param': str})
        old_df['view_param'] = old_df['view_param'].fillna('')
        old_legend = pd.read_excel(path, sheet_name='legend').fillna('')
        new_df_cmp = df.copy()
        new_df_cmp['view_param'] = new_df_cmp['view_param'].astype(str)
        data_unchanged = dataframes_equal(new_df_cmp, old_df, rtol=1e-6, verbose=False)
        legend_unchanged = dataframes_equal(legend_df, old_legend, verbose=False)
        if not data_unchanged:
            print(f'  {path.name}: data changed:')
            dataframes_equal(new_df_cmp, old_df, rtol=1e-6, verbose=True)
        if not legend_unchanged:
            print(f'  {path.name}: legend changed:')
            dataframes_equal(legend_df, old_legend, verbose=True)
    # Unreadable (not a spreadsheet, a truncated zip) or foreign (a sheet or column
    # missing): rewriting is the safe default. Anything else is a bug and raises.
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as e:
        print(f'  {path.name}: existing file unreadable ({e}); rewriting')
        return False
    return data_unchanged and legend_unchanged
