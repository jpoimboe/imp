"""parse_project_facts: project-file-only facts/guidance loader with file:line
errors and Norway-safe string scalars (spec 2026-06-15-project-facts-namespace)."""
from __future__ import annotations

from pathlib import Path

import pytest

from imp.engine.errors import LoadError
from imp.providers.config import parse_project_facts
from imp.providers.effective import load_effective_config


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "project.yaml"
    p.write_text(body)
    return p


def test_absent_file_returns_empty(tmp_path: Path) -> None:
    facts, guidance = parse_project_facts(tmp_path / "nope.yaml")
    assert facts == {}
    assert guidance == ""


def test_parses_facts_and_guidance(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        "facts:\n  test_command: make test\n  stack: python\n"
        "guidance: be careful\n",
    )
    facts, guidance = parse_project_facts(p)
    assert facts == {"test_command": "make test", "stack": "python"}
    assert guidance == "be careful"


def test_norway_value_stays_string(tmp_path: Path) -> None:
    p = _write(tmp_path, "facts:\n  flag: on\n  other: yes\n")
    facts, _ = parse_project_facts(p)
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


def test_reserved_guidance_key_rejected(tmp_path: Path) -> None:
    p = _write(tmp_path, "facts:\n  guidance: hi\n")
    with pytest.raises(LoadError) as ei:
        parse_project_facts(p)
    assert "reserved" in str(ei.value)


def test_numeric_value_coerced_to_string(tmp_path: Path) -> None:
    p = _write(tmp_path, "facts:\n  count: 42\n  ratio: 3.14\n")
    facts, _ = parse_project_facts(p)
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
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    (tmp_path / "imp").mkdir()
    (tmp_path / "imp" / "project.yaml").write_text(
        "workflow: default\nfacts:\n  test_command: make test\nguidance: hi\n"
    )
    cfg = load_effective_config(project_root=tmp_path, control_root=tmp_path).config
    assert cfg.project_facts == {"test_command": "make test"}
    assert cfg.guidance == "hi"


def test_user_layer_facts_not_contributed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    xdg = tmp_path / "xdg"
    (xdg / "imp").mkdir(parents=True)
    (xdg / "imp" / "config.yaml").write_text(
        "providers: [claude_code]\nfacts:\n  leaked: nope\n"
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    (tmp_path / "imp").mkdir()
    (tmp_path / "imp" / "project.yaml").write_text("workflow: default\n")
    cfg = load_effective_config(project_root=tmp_path, control_root=tmp_path).config
    assert cfg.project_facts == {}


def test_bool_fact_value_coerces_to_lowercase(tmp_path: Path) -> None:
    p = _write(tmp_path, "facts:\n  flag: true\n  off: false\n")
    facts, _ = parse_project_facts(p)
    assert facts["flag"] == "true"
    assert facts["off"] == "false"


def test_non_scalar_guidance_rejected(tmp_path: Path) -> None:
    p = _write(tmp_path, "guidance:\n  key: val\n")
    with pytest.raises(LoadError):
        parse_project_facts(p)
