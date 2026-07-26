"""parse_project_facts: facts-only loader with file:line errors, Norway-safe scalars."""
from __future__ import annotations

from pathlib import Path

import pytest

from imp.engine.errors import LoadError
from imp.providers.config import parse_project_facts
from imp.providers.effective import load_effective_config


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(body)
    return p


def test_absent_file_returns_empty(tmp_path: Path) -> None:
    facts = parse_project_facts(tmp_path / "nope.yaml")
    assert facts == {}


def test_parses_facts(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        "facts:\n  test_command: make test\n  stack: python\n",
    )
    facts = parse_project_facts(p)
    assert facts == {"test_command": "make test", "stack": "python"}


def test_norway_value_stays_string(tmp_path: Path) -> None:
    p = _write(tmp_path, "facts:\n  flag: on\n  other: yes\n")
    facts = parse_project_facts(p)
    assert facts["flag"] == "on"
    assert facts["other"] == "yes"


def test_non_identifier_key_rejected(tmp_path: Path) -> None:
    p = _write(tmp_path, "facts:\n  test-command: make test\n")
    with pytest.raises(LoadError) as ei:
        parse_project_facts(p)
    assert "test-command" in str(ei.value)


def test_non_scalar_leaf_rejected(tmp_path: Path) -> None:
    p = _write(tmp_path, "facts:\n  nested:\n    a: 1\n")
    with pytest.raises(LoadError):
        parse_project_facts(p)


def test_non_mapping_facts_rejected(tmp_path: Path) -> None:
    p = _write(tmp_path, "facts:\n  - a\n  - b\n")
    with pytest.raises(LoadError):
        parse_project_facts(p)


def test_numeric_value_coerced_to_string(tmp_path: Path) -> None:
    p = _write(tmp_path, "facts:\n  count: 42\n  ratio: 3.14\n")
    facts = parse_project_facts(p)
    assert facts["count"] == "42"
    assert facts["ratio"] == "3.14"


def test_error_carries_file_and_line(tmp_path: Path) -> None:
    p = _write(tmp_path, "facts:\n  bad-key: x\n")
    with pytest.raises(LoadError) as ei:
        parse_project_facts(p)
    assert ei.value.source is not None
    assert ei.value.source.file == p
    assert ei.value.source.line is not None


def test_facts_reach_config_from_project_layer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IMP_HOME", str(tmp_path / "imphome"))
    (tmp_path / "imp").mkdir()
    (tmp_path / "imp" / "config.yaml").write_text(
        "default-workflow: imp\nfacts:\n  test_command: make test\n"
    )
    cfg = load_effective_config(project_root=tmp_path, control_root=tmp_path).config
    assert cfg.project_facts == {"test_command": "make test"}


def test_user_layer_facts_not_contributed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    imp_home = tmp_path / "imphome"
    imp_home.mkdir(parents=True)
    (imp_home / "config.yaml").write_text(
        "providers: [claude_code]\nfacts:\n  leaked: nope\n"
    )
    monkeypatch.setenv("IMP_HOME", str(imp_home))
    (tmp_path / "imp").mkdir()
    (tmp_path / "imp" / "config.yaml").write_text("default-workflow: imp\n")
    cfg = load_effective_config(project_root=tmp_path, control_root=tmp_path).config
    assert cfg.project_facts == {}


def test_bool_fact_value_coerces_to_lowercase(tmp_path: Path) -> None:
    p = _write(tmp_path, "facts:\n  flag: true\n  off: false\n")
    facts = parse_project_facts(p)
    assert facts["flag"] == "true"
    assert facts["off"] == "false"


def test_leading_underscore_key_rejected(tmp_path: Path) -> None:
    p = _write(tmp_path, "facts:\n  _foo: bar\n")
    with pytest.raises(LoadError) as ei:
        parse_project_facts(p)
    assert "_foo" in str(ei.value)
    assert "not a valid identifier" in str(ei.value)
    assert "[A-Za-z][A-Za-z0-9_]*" in str(ei.value)
    assert "[A-Za-z_][A-Za-z0-9_]*" not in str(ei.value)
    assert ei.value.source is not None
    assert ei.value.source.file == p


def test_non_mapping_facts_error_points_at_facts_line(tmp_path: Path) -> None:
    p = _write(tmp_path, "# a comment\nfacts: notamap\n")
    with pytest.raises(LoadError) as ei:
        parse_project_facts(p)
    assert ei.value.source is not None
    assert ei.value.source.line == 2
