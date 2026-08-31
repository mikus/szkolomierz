"""What the map draws at a given zoom, and what it compares that against.

Thresholds come from measured extents rather than taste: Poland spans
lon 14.12-24.15, which fits a 1280px viewport at zoom ~7.5; a powiat is the
median child of a voivodeship and fills that viewport at ~11.6; a gmina at
~12.9. They are tunable - nothing downstream depends on the exact numbers - but
the ORDER and the one-level-up rule are not.

The comparison rule is the important half. A region is always scored against the
level above it, because scoring a voivodeship against its own voivodeship puts
every one of the sixteen at zero by construction and renders the national view
uniformly flat, at exactly the zoom where contrast matters most.
"""

LEVELS = ('country', 'voivodeship', 'powiat', 'gmina')

# (minimum zoom, level), coarsest first.
ZOOM_THRESHOLDS = ((12, 'gmina'), (10, 'powiat'), (8, 'voivodeship'), (0, 'country'))

_REFERENCE = {
    'country': 'national',
    'voivodeship': 'voivodeship',
    'powiat': 'powiat',
    'gmina': None,          # the selector decides; see the module docstring
}

_CHILD = {
    'country': 'voivodeship',
    'voivodeship': 'powiat',
    'powiat': 'gmina',
    'gmina': None,          # schools, not regions
}


def level_for_zoom(zoom: float) -> str:
    for minimum, level in ZOOM_THRESHOLDS:
        if zoom >= minimum:
            return level
    return 'country'


def reference_level_for(level: str):
    """The population a `level`'s regions are scored against, or None at the
    deepest level where the user's selector decides."""
    if level not in _REFERENCE:
        raise KeyError(f'unknown level {level!r}; expected one of {LEVELS}')
    return _REFERENCE[level]


def child_level_of(level: str):
    """The level whose regions are drawn at `level`, or None when schools are."""
    if level not in _CHILD:
        raise KeyError(f'unknown level {level!r}; expected one of {LEVELS}')
    return _CHILD[level]
