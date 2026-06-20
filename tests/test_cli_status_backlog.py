from __future__ import annotations

import argparse
from pathlib import Path

from imp.cli import main
from imp.engine import backlog
from imp.engine.change import Change
from tests.conftest import _seed_control


def test_backlog_rows_ready_and_blocked(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    backlog.write_record(tmp_path, change_id="2026-06-16-rdy111", title="Ready one",
                         description="b", depends_on=[], created_by="human")
    backlog.write_record(tmp_path, change_id="2026-06-16-blk222", title="Blocked one",
                         description="b", depends_on=["2026-06-16-missing"],
                         created_by="step:finalize")
    rows = main._backlog_rows(tmp_path)
    by_id = {r[0]: r for r in rows}
    assert by_id["2026-06-16-rdy111"][1] == "backlog"
    assert by_id["2026-06-16-rdy111"][2] == "ready"
    assert by_id["2026-06-16-rdy111"][4] == "Ready one"
    assert "blocked" in by_id["2026-06-16-blk222"][2]
    assert "2026-06-16-missing" in by_id["2026-06-16-blk222"][2]


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
    assert "Backlog" not in out  # no separate heading


def test_cmd_status_backlog_rows_appear_after_inflight(
        tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    live = Change.create(tmp_path, title="Live task", change_id="live111",
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
