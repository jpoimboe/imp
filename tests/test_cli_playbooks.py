from __future__ import annotations

from pathlib import Path

import jinja2
import pytest

from imp.cli import playbooks


def _write(control: Path, name: str, body: str, description: str = "d") -> None:
    d = control / "imp" / "playbooks"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(
        f"---\ndescription: {description}\n---\n{body}", encoding="utf-8")


def test_load_playbook_parses_description_and_body(tmp_path: Path) -> None:
    _write(tmp_path, "p", "hello body\n", description="my pb")
    pb = playbooks.find_playbook(tmp_path, "p")
    assert pb is not None
    assert pb.name == "p"
    assert pb.description == "my pb"
    assert pb.body.strip() == "hello body"


def test_render_substitutes_args(tmp_path: Path) -> None:
    _write(tmp_path, "p", "do {{ args }} now\n")
    pb = playbooks.find_playbook(tmp_path, "p")
    assert pb is not None
    assert playbooks.render_playbook(pb, "the thing") == "do the thing now\n"


def test_render_empty_args_blanks_the_variable(tmp_path: Path) -> None:
    _write(tmp_path, "p", "x={{ args }}=y\n")
    pb = playbooks.find_playbook(tmp_path, "p")
    assert pb is not None
    assert playbooks.render_playbook(pb, "") == "x==y\n"


def test_project_playbooks_take_precedence_over_shipped(tmp_path: Path) -> None:
    _write(tmp_path, "only-here", "local\n")
    names = {pb.name for pb in playbooks.list_playbooks(tmp_path)}
    assert names == {"only-here"}  # project dir present → shipped NOT merged in


def test_falls_back_to_shipped_when_no_project_dir(tmp_path: Path) -> None:
    names = {pb.name for pb in playbooks.list_playbooks(tmp_path)}
    assert {"ux", "interrupt", "dead-code"} <= names


def test_unknown_name_returns_none(tmp_path: Path) -> None:
    assert playbooks.find_playbook(tmp_path, "nope") is None


def test_load_playbook_without_frontmatter(tmp_path: Path) -> None:
    d = tmp_path / "imp" / "playbooks"
    d.mkdir(parents=True, exist_ok=True)
    (d / "raw.md").write_text("plain body\n", encoding="utf-8")
    pb = playbooks.find_playbook(tmp_path, "raw")
    assert pb is not None
    assert pb.description == ""
    assert pb.body == "plain body\n"


def test_render_raises_on_unknown_variable(tmp_path: Path) -> None:
    _write(tmp_path, "p", "{{ unknown }}\n")
    pb = playbooks.find_playbook(tmp_path, "p")
    assert pb is not None
    with pytest.raises(jinja2.UndefinedError):
        playbooks.render_playbook(pb, "x")


def test_find_playbook_strips_md_suffix(tmp_path: Path) -> None:
    _write(tmp_path, "dead-code", "body\n")
    pb_with = playbooks.find_playbook(tmp_path, "dead-code.md")
    pb_without = playbooks.find_playbook(tmp_path, "dead-code")
    assert pb_with is not None
    assert pb_with.name == pb_without.name


def test_load_raises_on_malformed_yaml_frontmatter(tmp_path: Path) -> None:
    d = tmp_path / "imp" / "playbooks"
    d.mkdir(parents=True, exist_ok=True)
    path = d / "bad.md"
    path.write_text("---\nfoo: [unclosed\n---\nbody\n", encoding="utf-8")
    with pytest.raises(ValueError, match="malformed"):
        playbooks._load(path)
