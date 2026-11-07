from __future__ import annotations

from pathlib import Path

from imp.cli import main
from imp.engine.quest import Quest
from tests.conftest import _seed_control


def _list_out(control: Path, monkeypatch, capsys) -> str:
    """Run `imp list` against `control`; return stdout."""
    monkeypatch.setattr(main, "_control_root", lambda: control)
    assert main.main(["list"]) == 0
    return capsys.readouterr().out


def test_list_shows_unmaterialized_home_in_backlog(
        tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    Quest.create_draft(tmp_path, title="Idea one", quest_id="2026-06-16-idea11",
                        base_branch="main", origin="idea", seed_opening="Idea one")
    out = _list_out(tmp_path, monkeypatch, capsys)
    assert "backlog" in out              # headed backlog section
    assert "2026-06-16-idea11" in out
    assert "idea" in out                 # provenance shown
    assert "Idea one" in out


def test_list_materialized_in_active_before_backlog(
        tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    Quest.create(tmp_path, title="Live", quest_id="live222", base_branch="main")
    Quest.create_draft(tmp_path, title="Backlog item", quest_id="2026-06-16-bk333",
                        base_branch="main", origin="idea", seed_opening="x")
    out = _list_out(tmp_path, monkeypatch, capsys)
    # Materialized change appears in the Active list, ABOVE the Backlog section;
    # the unmaterialized home never leaks into Active.
    assert out.index("live222") < out.index("backlog") < out.index("2026-06-16-bk333")


def test_list_blocked_backlog_home_shows_blocked(
        tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    Quest.create_draft(tmp_path, title="Blocked", quest_id="2026-06-16-blk44",
                        base_branch="main", origin="followup",
                        depends_on=["2026-06-16-ghost0"], seed_opening="x")
    out = _list_out(tmp_path, monkeypatch, capsys)
    assert "2026-06-16-blk44" in out
    assert "followup" in out
    assert "blocked" in out


def test_list_empty_prints_no_changes(
        tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    out = _list_out(tmp_path, monkeypatch, capsys)
    assert "backlog" not in out
    assert out.strip()
