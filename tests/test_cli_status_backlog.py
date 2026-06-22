from __future__ import annotations

import argparse
from pathlib import Path

from imp.cli import main
from imp.engine import backlog
from imp.engine.change import Change
from tests.conftest import _seed_control


def test_cmd_status_shows_backlog_row_by_default(
        tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    backlog.write_record(tmp_path, change_id="2026-06-16-rdy111", title="Ready one",
                         description="b", depends_on=[], created_by="human")
    rc = main._cmd_status(argparse.Namespace(all=False), tmp_path)
    assert rc == 0
    out = capsys.readouterr().out
    assert "2026-06-16-rdy111" in out
    assert "backlog" in out
    assert "Ready one" in out
    assert "Backlog" in out  # headed Backlog section


def test_cmd_status_backlog_rows_appear_after_inflight(
        tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    Change.create(tmp_path, title="Live task", change_id="live111",
                  base_branch="main")
    backlog.write_record(tmp_path, change_id="2026-06-16-bk222", title="Backlog task",
                         description="", depends_on=[], created_by="human")
    rc = main._cmd_status(argparse.Namespace(all=False), tmp_path)
    assert rc == 0
    out = capsys.readouterr().out
    assert "live111" in out
    assert "2026-06-16-bk222" in out
    # Inflight row must appear before backlog row in output.
    assert out.index("live111") < out.index("2026-06-16-bk222")


def test_cmd_status_empty_prints_no_changes(
        tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    rc = main._cmd_status(argparse.Namespace(all=False), tmp_path)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Backlog" not in out
    assert out.strip()
