from __future__ import annotations

import argparse
from pathlib import Path

from imp.cli import main
from imp.engine import backlog
from tests.conftest import _seed_control


def _args(**kw) -> argparse.Namespace:
    base = dict(backlog="wire OAuth login", depends_on=["2026-06-16-dep001"],
                title=None)
    base.update(kw)
    return argparse.Namespace(**base)


def test_new_backlog_records_and_prints_id(tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    rc = main._record_backlog(_args(), tmp_path)
    assert rc == 0
    ids = backlog.list_ids(tmp_path)
    assert len(ids) == 1
    rec = backlog.read_record(tmp_path, ids[0])
    assert rec is not None
    assert rec["description"] == "wire OAuth login"
    assert rec["depends_on"] == ["2026-06-16-dep001"]
    assert rec["created_by"] == "human"
    assert rec["title"] == ""
    out = capsys.readouterr().out
    assert ids[0] in out  # the new id is printed for the human


def test_new_backlog_no_deps(tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    main._record_backlog(_args(depends_on=None), tmp_path)
    rec = backlog.read_record(tmp_path, backlog.list_ids(tmp_path)[0])
    assert rec is not None
    assert rec["depends_on"] == []


def test_new_backlog_with_title(tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    ns = argparse.Namespace(backlog="b", depends_on=None, title="My Title")
    main._record_backlog(ns, tmp_path)
    rec = backlog.read_record(tmp_path, backlog.list_ids(tmp_path)[0])
    assert rec is not None
    assert rec["title"] == "My Title"


def test_new_backlog_argparse_wiring(tmp_path: Path, monkeypatch) -> None:
    parser = main._build_parser()
    args = parser.parse_args(["new", "--backlog", "my brief", "--depends-on", "2026-06-16-dep001"])
    assert args.backlog == "my brief"
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
        argparse.Namespace(backlog="b", depends_on=[], title=None,
                           base_branch=None, from_target=None,
                           non_interactive=False, message=None, tui=False,
                           seed=None, seed_file=None)))
    assert rc == 0
    assert captured
