from __future__ import annotations

from pathlib import Path

import pytest

import imp.cli.main as m
from imp.cli import playbooks


def _write_pb(control: Path, name: str, body: str) -> None:
    d = control / "imp" / "playbooks"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(
        f"---\ndescription: d\n---\n{body}", encoding="utf-8")


def test_new_parses_playbook_and_findings_only_flags() -> None:
    ns = m._build_parser().parse_args(
        ["new", "--playbook", "ux", "--findings-only", "focus on tui"])
    assert ns.playbook == "ux"
    assert ns.findings_only is True
    assert ns.opening == "focus on tui"


def test_resolve_playbook_seed_renders_args(tmp_path: Path) -> None:
    _write_pb(tmp_path, "p", "review {{ args }} please\n")
    seed = m.resolve_playbook_seed(tmp_path, "p", "the cli", findings_only=False)
    assert seed == "review the cli please\n"


def test_resolve_playbook_seed_appends_findings_directive(tmp_path: Path) -> None:
    _write_pb(tmp_path, "p", "body\n")
    seed = m.resolve_playbook_seed(tmp_path, "p", None, findings_only=True)
    assert seed.startswith("body\n")
    assert seed.endswith(playbooks.FINDINGS_ONLY_DIRECTIVE)


def test_resolve_playbook_seed_unknown_name_raises(tmp_path: Path) -> None:
    _write_pb(tmp_path, "p", "body\n")
    with pytest.raises(m.SeedError, match="unknown playbook 'nope'"):
        m.resolve_playbook_seed(tmp_path, "nope", None, findings_only=False)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_findings_only_without_playbook_exits_2(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m.worktree, "check_preconditions", lambda control: None)
    monkeypatch.setattr(m.worktree, "ensure_imp_ignored", lambda control: None)
    monkeypatch.setattr(m.worktree, "current_branch", lambda control: "main")
    ns = m._build_parser().parse_args(["new", "--findings-only", "do stuff"])
    rc = await m._async_cmd_new(ns)
    assert rc == 2
    assert "--findings-only requires --playbook" in capsys.readouterr().err


@pytest.mark.anyio
async def test_playbook_and_spec_file_are_exclusive(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m.worktree, "check_preconditions", lambda control: None)
    monkeypatch.setattr(m.worktree, "ensure_imp_ignored", lambda control: None)
    monkeypatch.setattr(m.worktree, "current_branch", lambda control: "main")
    ns = m._build_parser().parse_args(
        ["new", "--playbook", "ux", "--spec-file", "spec.md", "intent"])
    rc = await m._async_cmd_new(ns)
    assert rc == 2
    assert "--playbook and --spec-file are mutually exclusive" in capsys.readouterr().err
