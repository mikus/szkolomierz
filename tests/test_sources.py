"""Resolving the CIE manifest to the source files this project pins."""

import pytest

from school_quality.sources import (
    EDITION_PINS,
    local_filename,
    remote_filename,
    resolve_school_files,
)

MANIFEST = {
    'downloads': {
        'E8': {
            '2025': [
                {'label': 'szkoły', 'code': 'szkoly', 'date': '07.2025'},
                {'label': 'powiaty', 'code': 'powiaty', 'date': '07.2025'},
                {'label': 'szkoły', 'code': 'szkoly', 'date': '09.2025'},
            ],
            '2026': [
                {'label': 'szkoły', 'code': 'szkoly', 'date': '07.2026'},
            ],
            '2020': [
                {'label': 'gminy', 'code': 'gminy', 'date': '2020'},
            ],
        }
    }
}


def test_filenames_follow_the_cie_naming_scheme():
    assert remote_filename(2025, '07') == 'E8_2025_szkoly_07.xlsx'


def test_local_filename_starts_with_the_year():
    # The notebook loader accepts a file only if its name starts with 4 digits
    # (YEAR_FILE_RE) and takes the year from those digits.
    assert local_filename(2025, '07') == '2025 - E8_2025_szkoly_07.xlsx'


def test_resolves_only_the_pinned_edition_for_a_year_with_two():
    resolved = resolve_school_files(MANIFEST, pins={2025: '07'})
    assert [r['month'] for r in resolved] == ['07']
    assert resolved[0]['remote_name'] == 'E8_2025_szkoly_07.xlsx'


def test_picks_september_when_that_is_the_pin():
    resolved = resolve_school_files(MANIFEST, pins={2025: '09'})
    assert [r['month'] for r in resolved] == ['09']


def test_raises_when_a_pinned_year_has_no_school_level_entry():
    # 2020 in MANIFEST only has a 'gminy' (non-school) entry. A pin naming that
    # year should stop, not silently produce no file for it - the docstring's
    # "better to stop than to quietly fall back" applies to a missing edition
    # entirely, not just the wrong one.
    with pytest.raises(ValueError, match='2020'):
        resolve_school_files(MANIFEST, pins={2020: '2020'})


def test_raises_when_the_pinned_edition_is_absent():
    with pytest.raises(ValueError, match='2025'):
        resolve_school_files(MANIFEST, pins={2025: '11'})


def test_url_is_built_from_the_asset_base():
    resolved = resolve_school_files(MANIFEST, pins={2026: '07'})
    assert resolved[0]['url'].endswith('/assets/data/CSV/E8/2026/E8_2026_szkoly_07.xlsx')


def test_default_pins_are_the_target_editions():
    # September wherever it is published: those files are score revisions, and
    # the revisions are corrections. 2026 has only a July edition so far.
    assert EDITION_PINS == {2021: '09', 2022: '09', 2023: '09', 2024: '09',
                            2025: '09', 2026: '07'}
