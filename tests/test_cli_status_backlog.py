from __future__ import annotations

import argparse
from pathlib import Path

from imp.cli import main
from imp.engine.change import Change
from tests.conftest import _seed_control


def test_status_lists_unmaterialized_home_in_backlog(
        tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    Change.create_draft(tmp_path, title="Idea one", quest_id="2026-06-16-idea11",
                        base_branch="main", origin="idea", seed_opening="Idea one")
    rc = main._cmd_status(argparse.Namespace(all=False), tmp_path)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Backlog" in out              # headed Backlog section
    assert "2026-06-16-idea11" in out
    assert "idea" in out                 # provenance shown
    assert "Idea one" in out


def test_status_materialized_in_active_before_backlog(
        tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    Change.create(tmp_path, title="Live", quest_id="live222", base_branch="main")
    Change.create_draft(tmp_path, title="Backlog item", quest_id="2026-06-16-bk333",
                        base_branch="main", origin="idea", seed_opening="x")
    rc = main._cmd_status(argparse.Namespace(all=False), tmp_path)
    assert rc == 0
    out = capsys.readouterr().out
    # Materialized change appears in the Active list, ABOVE the Backlog section;
    # the unmaterialized home never leaks into Active.
    assert out.index("live222") < out.index("Backlog") < out.index("2026-06-16-bk333")


def test_status_blocked_backlog_home_shows_blocked(
        tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    Change.create_draft(tmp_path, title="Blocked", quest_id="2026-06-16-blk44",
                        base_branch="main", origin="followup",
                        depends_on=["2026-06-16-ghost0"], seed_opening="x")
    rc = main._cmd_status(argparse.Namespace(all=False), tmp_path)
    assert rc == 0
    out = capsys.readouterr().out
    assert "2026-06-16-blk44" in out
    assert "followup" in out
    assert "blocked" in out


def test_status_empty_prints_no_changes(
        tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    rc = main._cmd_status(argparse.Namespace(all=False), tmp_path)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Backlog" not in out
    assert out.strip()
