from __future__ import annotations

import argparse
from pathlib import Path

from imp.cli import main
from imp.engine import backlog
from tests.conftest import _seed_control


def _args(**kw) -> argparse.Namespace:
    base = dict(backlog="Wire OAuth login", description=None,
                depends_on=["2026-06-16-dep001"], title=None)
    base.update(kw)
    return argparse.Namespace(**base)


def test_new_backlog_records_title_and_empty_description(
        tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    rc = main._record_backlog(_args(), tmp_path)
    assert rc == 0
    rec = backlog.read_record(tmp_path, backlog.list_ids(tmp_path)[0])
    assert rec is not None
    assert rec["title"] == "Wire OAuth login"
    assert rec["description"] == ""
    assert rec["depends_on"] == ["2026-06-16-dep001"]
    assert rec["created_by"] == "human"
    out = capsys.readouterr().out
    assert rec["id"] in out


def test_new_backlog_description_populates_field(
        tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    main._record_backlog(_args(description="the longer brief"), tmp_path)
    rec = backlog.read_record(tmp_path, backlog.list_ids(tmp_path)[0])
    assert rec is not None
    assert rec["description"] == "the longer brief"


def test_new_backlog_no_deps(tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    main._record_backlog(_args(depends_on=None), tmp_path)
    rec = backlog.read_record(tmp_path, backlog.list_ids(tmp_path)[0])
    assert rec is not None
    assert rec["depends_on"] == []


def test_new_backlog_blank_title_errors_without_writing(
        tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    rc = main._record_backlog(_args(backlog="   "), tmp_path)
    assert rc == 2
    assert backlog.list_ids(tmp_path) == []
    assert "imp:" in capsys.readouterr().err


def test_new_backlog_with_title_flag_is_error(
        tmp_path: Path, monkeypatch, capsys) -> None:
    import asyncio
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    rc = asyncio.run(main._async_cmd_new(
        argparse.Namespace(backlog="A title", description=None, depends_on=[],
                           title="conflicting", base_branch=None, from_target=None,
                           non_interactive=True, message=None, tui=False,
                           opening=None, spec_file=None, playbook=None,
                           findings_only=False)))
    assert rc == 2
    assert backlog.list_ids(tmp_path) == []
    assert "imp:" in capsys.readouterr().err


def test_new_backlog_argparse_wiring(tmp_path: Path, monkeypatch) -> None:
    parser = main._build_parser()
    args = parser.parse_args(
        ["new", "--backlog", "My title", "--description", "longer",
         "--depends-on", "2026-06-16-dep001"])
    assert args.backlog == "My title"
    assert args.description == "longer"
    assert args.depends_on == ["2026-06-16-dep001"]


def test_async_cmd_new_routes_to_record_backlog(tmp_path: Path, monkeypatch) -> None:
    import asyncio
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    captured: list[tuple] = []

    def _fake_record(args, control):
        captured.append((args, control))
        return 0

    monkeypatch.setattr(main, "_record_backlog", _fake_record)
    rc = asyncio.run(main._async_cmd_new(
        argparse.Namespace(backlog="b", description=None, depends_on=[], title=None,
                           base_branch=None, from_target=None,
                           non_interactive=False, message=None, tui=False,
                           seed=None, seed_file=None)))
    assert rc == 0
    assert captured
