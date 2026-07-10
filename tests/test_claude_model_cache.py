from __future__ import annotations

import json
from pathlib import Path

from imp.providers.claude_model_cache import (
    ClaudeModelOption,
    read_claude_model_options,
)


def _write_cache(cfg_dir: Path, payload: object) -> None:
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / ".claude.json").write_text(json.dumps(payload))


def test_reads_options_from_config_dir(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    _write_cache(tmp_path, {
        "additionalModelOptionsCache": [
            {"value": "claude-fable-5[1m]", "label": "Fable 5",
             "description": "Most capable for your hardest tasks"},
            {"value": "claude-opus-4-8[1m]", "label": "Opus 4.8"},
        ],
    })
    opts = read_claude_model_options()
    assert opts == [
        ClaudeModelOption(
            value="claude-fable-5[1m]", label="Fable 5",
            description="Most capable for your hardest tasks"),
        ClaudeModelOption(
            value="claude-opus-4-8[1m]", label="Opus 4.8", description=None),
    ]


def test_missing_file_degrades_to_empty(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))  # no .claude.json written
    assert read_claude_model_options() == []


def test_malformed_json_degrades_to_empty(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    (tmp_path / ".claude.json").write_text("{not json")
    assert read_claude_model_options() == []


def test_missing_key_degrades_to_empty(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    _write_cache(tmp_path, {"other": 1})
    assert read_claude_model_options() == []


def test_non_list_cache_degrades_to_empty(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    _write_cache(tmp_path, {"additionalModelOptionsCache": {"value": "x"}})
    assert read_claude_model_options() == []


def test_skips_entries_without_a_string_value(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    _write_cache(tmp_path, {
        "additionalModelOptionsCache": [
            {"label": "no value"},
            "not a dict",
            {"value": 123},
            {"value": ""},
            {"value": "keep-me"},
        ],
    })
    assert read_claude_model_options() == [
        ClaudeModelOption(value="keep-me", label=None, description=None)]


def test_non_string_label_and_description_degrade_to_none(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    _write_cache(tmp_path, {
        "additionalModelOptionsCache": [
            {"value": "keep", "label": 42, "description": ["a", "b"]},
        ],
    })
    assert read_claude_model_options() == [
        ClaudeModelOption(value="keep", label=None, description=None)]
