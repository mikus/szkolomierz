"""Region keys derived from `Kod teryt gminy`.

Names cannot identify a region: 10 powiat names and 230 gmina names are borne by
more than one region, so every grouping keys on a TERYT prefix.

The gmina key is the first SIX digits. The seventh is the gmina type - 1 urban,
2 rural, 4 and 5 the town and countryside halves of an urban-rural gmina - and
not part of its identity. Keying on all seven splits a gmina's history when it
changes type (208 schools across 2021-2026) and counts 3,104 gminas where Poland
has 2,477.
"""

TERYT_LENGTH = 7
TERYT_LENGTH_NO_LEADING_ZERO = 6
LEVEL_WIDTHS = {'voivodeship': 2, 'powiat': 4, 'gmina': 6}


def normalise_teryt(value) -> str:
    """A 7-character zero-padded TERYT code. Raises on anything else.

    Spreadsheet round-trips turn 0401011 into 401011 or 401011.0, so padding is
    load-bearing rather than defensive. Valid inputs are exactly 6 or 7 digits: 7
    normally, 6 when a spreadsheet ate the leading zero of voivodeships 02/04/06/08.
    Anything shorter is not a truncated TERYT code, it is a different code
    entirely (e.g. a 2-digit voivodeship code on its own), and padding it would
    silently form a reference group for a region that does not exist.
    """
    if value is None:
        raise ValueError('TERYT code is missing')
    text = str(value).strip().removesuffix('.0')
    if not text or not text.isdigit() or len(text) not in (
        TERYT_LENGTH_NO_LEADING_ZERO, TERYT_LENGTH
    ):
        raise ValueError(f'not a TERYT gmina code: {value!r}')
    return text.zfill(TERYT_LENGTH)


def voivodeship_key(value) -> str:
    return normalise_teryt(value)[:LEVEL_WIDTHS['voivodeship']]


def powiat_key(value) -> str:
    return normalise_teryt(value)[:LEVEL_WIDTHS['powiat']]


def gmina_key(value) -> str:
    return normalise_teryt(value)[:LEVEL_WIDTHS['gmina']]


KEY_FUNCTIONS = {
    'voivodeship': voivodeship_key,
    'powiat': powiat_key,
    'gmina': gmina_key,
}
