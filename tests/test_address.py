"""Address selection: keep the fullest address a school ever reported.

The rules under test are documented in CLAUDE.md ("The address column is not
equally good every year" and "Address selection"). They exist because OKE's 2026
file ships a thinner address column — dropping the `ul.` marker and the leading
words of street names — so the newest year is not automatically the best source.
"""

import pandas as pd
import pytest

from school_quality.address import (
    address_tokens,
    collapse_redundant_street_type,
    dropped_words,
    is_shortened_address,
    select_address,
)

# ── collapse_redundant_street_type ───────────────────────────────────────────
# An abbreviation immediately followed by its own full form carries nothing the
# next word doesn't. OKE cleaned this up between 2024 and 2026, so collapsing it
# keeps one spelling per address across years.

@pytest.mark.parametrize('raw, expected', [
    ('al. Aleja Jana Pawła II 26a', 'Aleja Jana Pawła II 26a'),
    ('pl. Plac Wolności 3', 'Plac Wolności 3'),
    ('os. Osiedle Kolorowe 12', 'Osiedle Kolorowe 12'),
])
def test_collapses_an_abbreviation_followed_by_its_own_full_form(raw, expected):
    assert collapse_redundant_street_type(raw) == expected


def test_keeps_a_lone_ul_marker():
    # "ul." is the only street-type marker present, so it carries information.
    assert collapse_redundant_street_type('ul. Kopernika 5') == 'ul. Kopernika 5'


def test_keeps_an_abbreviation_not_followed_by_its_full_form():
    assert collapse_redundant_street_type('al. Jana Pawła II 26a') == 'al. Jana Pawła II 26a'


def test_treats_missing_street_as_empty_string():
    assert collapse_redundant_street_type(None) == ''


# ── address_tokens ───────────────────────────────────────────────────────────
# Tokens split on punctuation as well as spaces, so spelling variants of the
# same street tokenise identically.

def test_hyphenated_and_spaced_street_names_tokenise_identically():
    assert address_tokens('Grota-Roweckiego 5') == address_tokens('Grota Roweckiego 5')


def test_abbreviations_tokenise_identically_regardless_of_spacing():
    assert address_tokens('Ks.Kan.E.Sierbińskiego') == address_tokens('ks. kan. E. Sierbińskiego')


def test_tokens_are_lowercased():
    assert address_tokens('Szkolna 1') == ('szkolna', '1')


def test_no_tokens_for_a_missing_street():
    assert address_tokens(None) == ()


# ── is_shortened_address ─────────────────────────────────────────────────────
# True means the candidate says strictly less than the current value about the
# same place, so the fuller current value should be kept.

def test_dropping_the_ul_prefix_is_a_shortened_form():
    assert is_shortened_address(('Radom', 'Maja 27'), ('Radom', 'ul. 3 Maja 27')) is True


def test_dropping_leading_words_of_the_street_is_a_shortened_form():
    assert is_shortened_address(
        ('Radom', 'Mickiewicza 126/128'),
        ('Radom', 'ul. Adama Mickiewicza 126/128'),
    ) is True


def test_a_different_town_is_a_real_move_not_a_shortening():
    assert is_shortened_address(('Kielce', 'Maja 27'), ('Radom', 'ul. 3 Maja 27')) is False


def test_a_changed_house_number_is_a_real_change():
    # Without the house-number rule "Krynoliny 9" would count as a shortened
    # form of "Krynoliny 9/11" and the renumbering would be silently discarded.
    assert is_shortened_address(('Radom', 'Krynoliny 9'), ('Radom', 'Krynoliny 9/11')) is False


def test_a_house_number_that_is_a_prefix_of_another_is_a_real_change():
    assert is_shortened_address(('Radom', 'Szkolna 1'), ('Radom', 'Szkolna 12')) is False


def test_a_different_street_is_a_real_change():
    assert is_shortened_address(('Radom', 'Polna 5'), ('Radom', 'ul. Kopernika 5')) is False


def test_street_words_must_form_a_contiguous_run():
    # "Adama 126" takes its words from the current street but not consecutively.
    assert is_shortened_address(
        ('Radom', 'Adama 126'),
        ('Radom', 'ul. Adama Mickiewicza 126'),
    ) is False


def test_an_equal_length_street_is_not_a_shortening():
    assert is_shortened_address(('Radom', 'ul. Kopernika 5'), ('Radom', 'ul. Kopernika 5')) is False


def test_a_longer_street_is_not_a_shortening():
    assert is_shortened_address(
        ('Radom', 'ul. Mikołaja Kopernika 5'),
        ('Radom', 'ul. Kopernika 5'),
    ) is False


@pytest.mark.parametrize('candidate, current', [
    (('Radom', ''), ('Radom', 'ul. Kopernika 5')),
    (('Radom', 'ul. Kopernika 5'), ('Radom', '')),
])
def test_a_missing_street_on_either_side_is_never_a_shortening(candidate, current):
    assert is_shortened_address(candidate, current) is False


def test_town_and_street_are_compared_separately():
    # Concatenating them would break contiguity when "ul." sits at the boundary:
    # (raczyny, ul, kopernika, 5) vs (raczyny, kopernika, 5) is not a run.
    assert is_shortened_address(
        ('Raczyny', 'Kopernika 5'),
        ('Raczyny', 'ul. Kopernika 5'),
    ) is True


# ── select_address ───────────────────────────────────────────────────────────

def _group(rows):
    """Build the per-school frame select_address expects."""
    return pd.DataFrame(
        [{'rspo': 1, 'school_name': 'SP 1', 'year': y, 'miejscowosc': t, 'ulica_nr': s}
         for y, t, s in rows]
    )


def test_keeps_the_fuller_address_when_a_later_year_shortens_it():
    chosen, declined = select_address(_group([
        (2025, 'Radom', 'ul. 3 Maja 27'),
        (2026, 'Radom', 'Maja 27'),
    ]))
    assert chosen == ('Radom', 'ul. 3 Maja 27')
    assert [d['year'] for d in declined] == [2026]


def test_accepts_a_genuine_move_from_a_later_year():
    chosen, declined = select_address(_group([
        (2025, 'Radom', 'ul. Kopernika 5'),
        (2026, 'Kielce', 'ul. Polna 1'),
    ]))
    assert chosen == ('Kielce', 'ul. Polna 1')
    assert declined == []


def test_walks_years_oldest_to_newest_regardless_of_row_order():
    chosen, _ = select_address(_group([
        (2026, 'Radom', 'Maja 27'),
        (2025, 'Radom', 'ul. 3 Maja 27'),
    ]))
    assert chosen == ('Radom', 'ul. 3 Maja 27')


def test_a_single_year_is_taken_as_is():
    chosen, declined = select_address(_group([(2026, 'Radom', 'Maja 27')]))
    assert chosen == ('Radom', 'Maja 27')
    assert declined == []


def test_a_shortened_year_does_not_block_a_later_genuine_move():
    # The 2026 move is judged against the address kept in 2025, not against the
    # shortened 2026-style value that was declined in between.
    chosen, declined = select_address(_group([
        (2024, 'Radom', 'ul. 3 Maja 27'),
        (2025, 'Radom', 'Maja 27'),
        (2026, 'Kielce', 'ul. Polna 1'),
    ]))
    assert chosen == ('Kielce', 'ul. Polna 1')
    assert [d['year'] for d in declined] == [2025]


def test_a_declined_row_records_what_was_kept_and_what_was_offered():
    _, declined = select_address(_group([
        (2025, 'Radom', 'ul. 3 Maja 27'),
        (2026, 'Radom', 'Maja 27'),
    ]))
    assert declined == [{
        'rspo': 1,
        'school_name': 'SP 1',
        'year': 2026,
        'kept_miejscowosc': 'Radom',
        'kept_ulica_nr': 'ul. 3 Maja 27',
        'declined_miejscowosc': 'Radom',
        'declined_ulica_nr': 'Maja 27',
    }]


def test_the_chosen_address_has_redundant_street_types_collapsed():
    chosen, _ = select_address(_group([(2024, 'Radom', 'al. Aleja Jana Pawła II 26a')]))
    assert chosen == ('Radom', 'Aleja Jana Pawła II 26a')


# ── dropped_words ────────────────────────────────────────────────────────────
# The rejected-addresses report names the words that would have been lost,
# rather than repeating an identical reason on every row.

def test_names_the_words_the_shorter_address_would_have_lost():
    assert dropped_words('ul. Adama Mickiewicza 126/128', 'Mickiewicza 126/128') == 'ul adama'


def test_no_words_lost_when_the_addresses_share_all_tokens():
    assert dropped_words('Kopernika 5', 'Kopernika 5') == ''
