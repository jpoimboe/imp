from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from imp.cli import main
from imp.engine import backlog
from tests.conftest import _seed_control


def test_backlog_status_lines_ready_and_blocked(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    backlog.write_record(tmp_path, change_id="2026-06-16-rdy111", title="Ready one",
                         brief="b", depends_on=[], created_by="human")
    backlog.write_record(tmp_path, change_id="2026-06-16-blk222", title="Blocked one",
                         brief="b", depends_on=["2026-06-16-missing"],
                         created_by="step:finalize")

    lines = main._backlog_status_lines(tmp_path, now=datetime.now(UTC))
    text = "\n".join(lines)
    assert "2026-06-16-rdy111" in text
    assert "Ready one" in text
    assert "ready" in text.lower()
    assert "2026-06-16-blk222" in text
    assert "Blocked one" in text
    assert "blocked" in text.lower()
    assert "2026-06-16-missing" in text     # blocked names the unmerged dep
    assert "human" in text and "step:finalize" in text  # created_by surfaced


def test_backlog_status_lines_empty(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    assert main._backlog_status_lines(tmp_path, now=datetime.now(UTC)) == []


def test_cmd_status_prints_backlog_section(tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    backlog.write_record(tmp_path, change_id="2026-06-16-rdy111", title="Ready one",
                         brief="b", depends_on=[], created_by="human")
    import argparse
    rc = main._cmd_status(argparse.Namespace(all=False), tmp_path)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Backlog" in out
    assert "2026-06-16-rdy111" in out


def test_cmd_status_no_changes_no_backlog_prints_message(tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    import argparse
    rc = main._cmd_status(argparse.Namespace(all=False), tmp_path)
    assert rc == 0
    out = capsys.readouterr().out
    # No changes and no backlog → prints the no-changes message (not a blank "Backlog" section)
    assert "Backlog" not in out
    assert out.strip()  # some message printed
