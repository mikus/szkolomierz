"""Pick one address per school across years, keeping the fullest one reported.

A newer file is not automatically a better source. From 2026 OKE ships a thinner
address column — it drops the `ul.` marker and the leading words of a street name
('ul. 3 Maja 27' -> 'Maja 27'). Taking the newest year blindly would discard
street names we already have, degrade the map popup, and invalidate rows of the
geocoding cache without adding information.

Rule: walk each school's years oldest -> newest, keeping a current address. A
newer address replaces the current one UNLESS it is a *shortened form* of it. A
shortened form says strictly less about the same place, so the fuller address
stays. Anything else — a different street, house number, or town — is a real
change and wins.
"""

import re

# An abbreviation immediately followed by its own full form carries nothing the
# next word doesn't ('al. Aleja Jana Pawła II 26a'). OKE cleaned this up between
# 2024 and 2026, so collapsing it keeps one spelling per address across years.
# A lone 'ul.' is left alone — it is the only street-type marker present.
REDUNDANT_STREET_TYPE_RE = re.compile(
    r'^\s*(al\.|pl\.|os\.)\s+(?=(aleja|plac|osiedle)\b)', re.IGNORECASE)
ADDRESS_TOKEN_SPLIT_RE = re.compile(r'[^0-9a-ząćęłńóśźż]+', re.IGNORECASE)


def collapse_redundant_street_type(ulica_nr) -> str:
    return REDUNDANT_STREET_TYPE_RE.sub('', str(ulica_nr or '')).strip()


def address_tokens(text) -> tuple:
    """Lowercased alphanumeric words, split on punctuation as well as spaces.

    So 'Grota-Roweckiego' and 'Grota Roweckiego' tokenise identically, as do
    'Ks.Kan.E.Sierbińskiego' and 'ks. kan. E. Sierbińskiego'.
    """
    return tuple(t for t in ADDRESS_TOKEN_SPLIT_RE.split(str(text or '').lower()) if t)


def is_shortened_address(candidate, current) -> bool:
    """True if `candidate` says strictly less than `current` about the same place.

    Town and street are compared SEPARATELY: concatenating them would break the
    contiguity test whenever 'ul.' sits at the boundary — 'Raczyny | ul. Kopernika 5'
    vs 'Raczyny | Kopernika 5' tokenises to (raczyny, ul, kopernika, 5) against
    (raczyny, kopernika, 5), where the shorter is not a contiguous run.

    Requiring the last token (the house number) to match keeps genuine
    renumberings out: 'Krynoliny 9' is NOT a shortened form of 'Krynoliny 9/11',
    and 'Szkolna 1' is not one of 'Szkolna 12'.
    """
    candidate_town, candidate_street = address_tokens(candidate[0]), address_tokens(candidate[1])
    current_town, current_street = address_tokens(current[0]), address_tokens(current[1])

    if candidate_town != current_town:
        return False                                  # a different town is a real move
    if not candidate_street or not current_street:
        return False
    if len(candidate_street) >= len(current_street):
        return False
    if candidate_street[-1] != current_street[-1]:
        return False                                  # house number changed -> real change
    return any(current_street[i:i + len(candidate_street)] == candidate_street
               for i in range(len(current_street) - len(candidate_street) + 1))


def select_address(group):
    """Pick one (miejscowosc, ulica_nr) for a school; also return what was declined.

    The walk is stateful (each year is judged against the address chosen so far),
    so it resists a groupby/vectorised form.
    """
    current, declined = None, []
    for _, row in group.sort_values('year').iterrows():
        candidate = (row['miejscowosc'], collapse_redundant_street_type(row['ulica_nr']))
        if current is None:
            current = candidate
        elif is_shortened_address(candidate, current):
            declined.append({
                'rspo':                 row['rspo'],
                'school_name':          row['school_name'],
                'year':                 row['year'],
                'kept_miejscowosc':     current[0],
                'kept_ulica_nr':        current[1],
                'declined_miejscowosc': candidate[0],
                'declined_ulica_nr':    candidate[1],
            })
        else:
            current = candidate
    return current, declined


def dropped_words(kept_street, declined_street) -> str:
    """The words the declined address would have lost, for the rejection report.

    The reason is identical on every row of that report, so naming the lost words
    is more useful than repeating a constant comment.
    """
    declined_tokens = set(address_tokens(declined_street))
    return ' '.join(t for t in address_tokens(kept_street) if t not in declined_tokens)
