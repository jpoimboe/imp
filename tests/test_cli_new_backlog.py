from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from imp.cli import main
from imp.engine.change import Change
from tests.conftest import _seed_control


def _args(**kw) -> argparse.Namespace:
    base = dict(idea="Wire OAuth login", opening=None,
                depends_on=["2026-06-16-dep001"], title=None, workflow=None)
    base.update(kw)
    return argparse.Namespace(**base)


def _only_new_id(control: Path) -> str:
    return next(p.name for p in (control / ".imp" / "changes").iterdir())


def test_new_idea_creates_change_home_with_origin_idea(
        tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    rc = main._record_idea(_args(), tmp_path)
    assert rc == 0
    new_id = _only_new_id(tmp_path)
    change = Change.load(tmp_path, new_id)
    assert change.materialized is False
    assert change.origin == "idea"
    assert change.title == "Wire OAuth login"
    assert change.seed_opening == "Wire OAuth login"
    meta = json.loads((change.change_dir / "change.json").read_text())
    assert meta["depends_on"] == ["2026-06-16-dep001"]
    assert new_id in capsys.readouterr().out


def test_new_idea_description_populates_seed_opening(
        tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    main._record_idea(_args(opening="the longer brief"), tmp_path)
    change = Change.load(tmp_path, _only_new_id(tmp_path))
    assert change.seed_opening == "Wire OAuth login\n\nthe longer brief"


def test_new_idea_no_deps(tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    main._record_idea(_args(depends_on=None), tmp_path)
    meta = json.loads((Change.load(tmp_path, _only_new_id(tmp_path)).change_dir
                       / "change.json").read_text())
    assert meta["depends_on"] == []


def test_new_idea_blank_title_errors_without_creating(
        tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    rc = main._record_idea(_args(idea="   "), tmp_path)
    assert rc == 2
    assert not (tmp_path / ".imp" / "changes").exists()
    captured = capsys.readouterr()
    assert "imp:" in captured.err
    assert "--backlog" in captured.err


def test_new_idea_with_title_flag_is_error(
        tmp_path: Path, monkeypatch, capsys) -> None:
    import asyncio
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    rc = asyncio.run(main._async_cmd_new(
        argparse.Namespace(idea="A title", opening=None, depends_on=[],
                           title="conflicting", base_branch=None,
                           non_interactive=True, message=None, tui=False,
                           workflow=None)))
    assert rc == 2
    assert not (tmp_path / ".imp" / "changes").exists()
    assert "--backlog" in capsys.readouterr().err


def test_new_backlog_argparse_wiring(tmp_path: Path) -> None:
    parser = main._build_parser()
    args = parser.parse_args(
        ["new", "--backlog", "My title", "longer brief",
         "--depends-on", "2026-06-16-dep001"])
    assert args.idea == "My title"
    assert args.opening == "longer brief"
    assert args.depends_on == ["2026-06-16-dep001"]


def test_run_subcommand_is_unknown() -> None:
    parser = main._build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["run", "ux"])


def test_new_rejects_playbook_flags() -> None:
    parser = main._build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["new", "--playbook", "ux"])
    with pytest.raises(SystemExit):
        parser.parse_args(["new", "--findings-only"])


def test_async_cmd_new_routes_to_record_idea(tmp_path: Path, monkeypatch) -> None:
    import asyncio
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    captured: list[tuple] = []

    def _fake_record(args, control):
        captured.append((args, control))
        return 0

    monkeypatch.setattr(main, "_record_idea", _fake_record)
    rc = asyncio.run(main._async_cmd_new(
        argparse.Namespace(idea="b", opening=None, depends_on=[], title=None,
                           base_branch=None, non_interactive=False, message=None,
                           autonomous=False, tui=False, workflow=None)))
    assert rc == 0
    assert captured


def test_interactive_backlog_files_instantly_no_session(
        tmp_path: Path, monkeypatch) -> None:
    import sys

    import anyio

    from imp.cli import main as cli_main

    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)

    async def _no_session(*a, **k):
        raise AssertionError("backlog must not run a session")

    monkeypatch.setattr(cli_main, "run_session_client", _no_session)
    monkeypatch.setattr(cli_main, "_run_tui_session", _no_session)

    orig_in, orig_out = sys.stdin.isatty, sys.stdout.isatty
    sys.stdin.isatty = lambda: True   # type: ignore[method-assign]
    sys.stdout.isatty = lambda: True  # type: ignore[method-assign]
    try:
        rc = anyio.run(cli_main._async_cmd_new, argparse.Namespace(
            idea="My backlog item", opening="a longer brief", title=None,
            non_interactive=False, message=None, autonomous=False, tui=False,
            depends_on=["2026-06-16-dep001"], workflow=None))
    finally:
        sys.stdin.isatty = orig_in    # type: ignore[method-assign]
        sys.stdout.isatty = orig_out  # type: ignore[method-assign]

    assert rc == 0
    change = Change.load(tmp_path, _only_new_id(tmp_path))
    assert change.origin == "idea"
    assert change.title == "My backlog item"
    assert change.materialized is False
    assert change.seed_opening == "My backlog item\n\na longer brief"
