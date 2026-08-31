"""Coordinates from the RSPO register, keyed by school id.

RSPO is the Polish register of schools. Its institution detail record carries a
geotag, so a school's coordinates can be looked up by its RSPO id rather than by
geocoding its address text. That matters here for a reason beyond speed: the
source's address column degraded in 2026, dropping street-name words for
hundreds of schools, and an id-keyed lookup is immune to that.

This module is the pure half - URL construction and payload parsing. The fetch
itself lives in scripts/geocode_schools.py, so this stays testable without a
network.
"""

RSPO_DETAIL_URL = 'https://rspo.gov.pl/api/Institution/{rspo}'

# Poland's bounding box, generously rounded. A geotag outside it is not a
# Polish school's location - most often 0,0 from an upstream default.
POLAND_LAT_MIN, POLAND_LAT_MAX = 48.9, 55.0
POLAND_LON_MIN, POLAND_LON_MAX = 14.0, 24.3


def rspo_detail_url(rspo: int) -> str:
    return RSPO_DETAIL_URL.format(rspo=int(rspo))


def geotag_from_payload(payload: dict):
    """(lat, lon) from an RSPO institution record, or None.

    None means "no usable coordinate here" for every reason - absent, blank,
    unparseable, or implausible - because every one of them has the same
    consequence: fall through to the address geocoder.
    """
    if not isinstance(payload, dict):
        return None
    tag = payload.get('hqAddressGeotag') or {}
    if not isinstance(tag, dict):
        return None
    try:
        lat = float(tag.get('latitude'))
        lon = float(tag.get('longitude'))
    except (TypeError, ValueError):
        return None
    if not (POLAND_LAT_MIN <= lat <= POLAND_LAT_MAX):
        return None
    if not (POLAND_LON_MIN <= lon <= POLAND_LON_MAX):
        return None
    return (lat, lon)
