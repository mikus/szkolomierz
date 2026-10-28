"""I/O helpers shared by the export notebooks: JSON, the skip/write decision, xlsx."""

import json

import pandas as pd
import pytest

from export_io import (
    COMPACT_JSON,
    commit_export,
    dataframes_equal,
    dumps_json,
    json_payloads_equal,
    write_json,
    write_xlsx,
    xlsx_unchanged,
)


def test_dumps_json_is_compact_and_keeps_polish_letters():
    assert COMPACT_JSON == (',', ':')
    assert dumps_json({'a': [1, 'żółw']}) == '{"a":[1,"żółw"]}'


def test_dumps_json_refuses_nan():
    # json.dumps would write a bare NaN, which the browser's JSON.parse rejects.
    with pytest.raises(ValueError):
        dumps_json({'pass_rate': float('nan')})


def test_write_json_round_trips(tmp_path):
    path = tmp_path / 'x.json'
    write_json(path, {'name': 'Łódź', 'n': 3})
    assert path.read_text(encoding='utf-8') == '{"name":"Łódź","n":3}'
    assert json.loads(path.read_text(encoding='utf-8')) == {'name': 'Łódź', 'n': 3}


def test_payloads_equal_ignores_only_generated_at(tmp_path):
    path = tmp_path / 'p.json'
    write_json(path, {'metadata': {'generated_at': '2026-01-01T00:00:00', 'k': 1}, 'v': 2})
    assert json_payloads_equal({'metadata': {'generated_at': 'later', 'k': 1}, 'v': 2}, path)
    assert not json_payloads_equal({'metadata': {'generated_at': 'later', 'k': 1}, 'v': 3}, path)
    assert not json_payloads_equal({'metadata': {'generated_at': 'later', 'k': 2}, 'v': 2}, path)


def test_payloads_equal_is_false_for_a_missing_or_broken_file(tmp_path):
    assert not json_payloads_equal({'v': 1}, tmp_path / 'missing.json')
    broken = tmp_path / 'broken.json'
    broken.write_text('{"v": 1', encoding='utf-8')
    assert not json_payloads_equal({'v': 1}, broken)


def test_commit_export_requires_both_run_flags(tmp_path):
    with pytest.raises(TypeError):
        commit_export(tmp_path / 'f', unchanged=True, write=lambda: None)
    with pytest.raises(TypeError):
        commit_export(tmp_path / 'f', unchanged=True, write=lambda: None, force_regenerate=False)


def _writer(path, calls):
    def write():
        calls.append(path.name)
        path.write_text('x', encoding='utf-8')
    return write


def test_commit_export_skips_an_unchanged_file(tmp_path):
    calls = []
    path = tmp_path / 'f.json'
    result = commit_export(path, unchanged=True, write=_writer(path, calls),
                           force_regenerate=False, write_output_files=True)
    assert result == 'skipped'
    assert calls == []


def test_commit_export_writes_a_changed_file_or_a_forced_one(tmp_path):
    calls = []
    path = tmp_path / 'f.json'
    assert commit_export(path, unchanged=False, write=_writer(path, calls),
                         force_regenerate=False, write_output_files=True) == 'wrote'
    assert commit_export(path, unchanged=True, write=_writer(path, calls),
                         force_regenerate=True, write_output_files=True) == 'wrote'
    assert calls == ['f.json', 'f.json']


def test_commit_export_suppresses_a_write_when_output_files_are_off(tmp_path):
    calls = []
    path = tmp_path / 'f.json'
    result = commit_export(path, unchanged=False, write=_writer(path, calls),
                           force_regenerate=False, write_output_files=False)
    assert result == 'suppressed'
    assert calls == []
    assert not path.exists()


def test_dataframes_equal_compares_floats_with_tolerance():
    a = pd.DataFrame({'k': ['x', 'y'], 'v': [1.0, float('nan')]})
    assert dataframes_equal(a, pd.DataFrame({'k': ['x', 'y'], 'v': [1.0 + 1e-9, float('nan')]}))
    assert not dataframes_equal(a, pd.DataFrame({'k': ['x', 'y'], 'v': [1.1, float('nan')]}),
                                verbose=False)
    assert not dataframes_equal(a, pd.DataFrame({'k': ['x', 'z'], 'v': [1.0, float('nan')]}),
                                verbose=False)
    assert not dataframes_equal(a, a.rename(columns={'v': 'w'}), verbose=False)


DATA = pd.DataFrame({'rspo': [1, 2], 'view_param': ['', '2024'], 'score': [0.5, 0.25]})
LEGEND = pd.DataFrame({'key': ['metric'], 'description': ['mean']})


def test_xlsx_unchanged_after_write_xlsx(tmp_path):
    path = tmp_path / 'f.xlsx'
    write_xlsx(DATA, LEGEND, path)
    assert xlsx_unchanged(path, DATA, LEGEND)


def test_xlsx_unchanged_sees_a_legend_only_edit(tmp_path):
    # Commit 3f2357a's defect: comparing `data` alone never let a legend edit reach the file.
    path = tmp_path / 'f.xlsx'
    write_xlsx(DATA, LEGEND, path)
    edited = LEGEND.assign(description=['median'])
    assert not xlsx_unchanged(path, DATA, edited)


def test_xlsx_unchanged_sees_a_data_edit_and_a_missing_file(tmp_path):
    path = tmp_path / 'f.xlsx'
    write_xlsx(DATA, LEGEND, path)
    assert not xlsx_unchanged(path, DATA.assign(score=[0.5, 0.75]), LEGEND)
    assert not xlsx_unchanged(tmp_path / 'missing.xlsx', DATA, LEGEND)


def test_an_unreadable_or_foreign_xlsx_counts_as_changed(tmp_path):
    garbage = tmp_path / 'garbage.xlsx'
    garbage.write_bytes(b'not a spreadsheet')
    assert not xlsx_unchanged(garbage, DATA, LEGEND)
    no_legend = tmp_path / 'no_legend.xlsx'
    DATA.to_excel(no_legend, sheet_name='data', index=False)
    assert not xlsx_unchanged(no_legend, DATA, LEGEND)
