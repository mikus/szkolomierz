"""The three-row header of CIE's school-level xlsx (sheet `SAS`) as column keys.

Row 0 names a band (`.`, `egzaminy obowiazkowe`, `egzaminy dodatkowe`,
`zestawy zadan w jezyku obcym`) in its first column only, row 1 a subject in its
first column only, and row 2 the measure in every column. The meta columns
(RSPO, name, address, type, ...) come first, with only row 2 filled.

Every column gets one key. Meta columns - those before the first band cell -
become ('meta', '', label). Finding that boundary explicitly, rather than
forward-filling across the whole row, is what keeps meta columns out of the
data; a column with no row-2 label raises instead of being folded into the
subject before it. Data columns become (band, subject, measure). The band is
part of the key because one subject name can sit in two bands (`jezyk angielski
ustny (m)` is in both `egzaminy obowiazkowe` and `egzaminy dodatkowe`). Subject
names are kept whole, suffix included: `matematyka poziom podstawowy` with no
suffix is maths sat in Lithuanian, a different exam from `matematyka poziom
podstawowy (m)`, and stripping suffixes would merge the two. Which subjects
matter is `matura.py`'s decision; this module keys every column and drops none.
"""

import re
import unicodedata
from collections import Counter

import pandas as pd

HEADER_ROWS = 3
META = 'meta'
COLUMN_LEVELS = ['band', 'subject', 'measure']


def normalize_label(value) -> str:
    """A header cell as a comparable label: ASCII, lowercase, single-spaced.

    A blank cell (None, NaN, or a pandas 'Unnamed: ...' placeholder) is ''. The
    leading '*' CIE puts on some measures ('* liczba zdających') is dropped, and
    so is the trailing newline on 'typ placówki' and 'rodzaj placówki'.
    """
    if not isinstance(value, str) and pd.isna(value):
        return ''
    text = str(value)
    if text.startswith('Unnamed:'):
        return ''
    text = unicodedata.normalize('NFKD', text)
    text = ''.join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace('ł', 'l').replace('Ł', 'L')  # ł has no NFKD decomposition
    text = re.sub(r'\s+', ' ', text).lower().strip()  # \s+ covers the newlines
    return text.removeprefix('*').strip()


def parse_header(top: pd.DataFrame) -> list[tuple[str, str, str]]:
    """One key per column of `top`, the first three rows of a sheet read with header=None.

    Meta columns are ('meta', '', label); data columns are (band, subject,
    measure), the band carried forward from row 0 and the subject from row 1 -
    reset when a new band starts - and the measure taken from row 2.

    Raises ValueError when a column has a blank row-2 label, when a band starts
    without a subject, or when two columns get the same key (naming it).
    """
    bands = [normalize_label(value) for value in top.iloc[0]]
    subjects = [normalize_label(value) for value in top.iloc[1]]
    measures = [normalize_label(value) for value in top.iloc[2]]
    first_band = next((i for i, band in enumerate(bands) if band), len(bands))

    keys = []
    band = subject = ''
    for i, (band_cell, subject_cell, measure) in enumerate(zip(bands, subjects, measures)):
        if not measure:
            raise ValueError(f'header column {i} has a blank row-2 label')
        if i < first_band:
            keys.append((META, '', measure))
            continue
        if band_cell:
            band, subject = band_cell, ''
        if subject_cell:
            subject = subject_cell
        if not subject:
            raise ValueError(f'header column {i} (band {band!r}) has no subject')
        keys.append((band, subject, measure))

    duplicates = sorted(key for key, count in Counter(keys).items() if count > 1)
    if duplicates:
        raise ValueError(f'duplicate header keys: {duplicates}')
    return keys


def apply_header(raw: pd.DataFrame) -> pd.DataFrame:
    """The data rows of a sheet read with header=None, under `parse_header`'s keys.

    Values are left as read: with the header rows in the same columns, every
    column came in as text or object, so convert with pd.to_numeric where a
    number is expected.
    """
    keys = parse_header(raw.iloc[:HEADER_ROWS])
    data = raw.iloc[HEADER_ROWS:].reset_index(drop=True)
    data.columns = pd.MultiIndex.from_tuples(keys, names=COLUMN_LEVELS)
    return data
