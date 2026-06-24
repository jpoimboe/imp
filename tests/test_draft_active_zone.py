from __future__ import annotations

from pathlib import Path

from imp.cli import main as m
from imp.engine.change import Change
from imp.engine.state import State
from tests.conftest import _seed_control


def _park_draft(tmp_path: Path, cid: str, origin: str = "draft") -> Change:
    ch = Change.create_draft(tmp_path, title="X", change_id=cid,
                             base_branch="main", origin=origin)
    st = State.load(ch.state_dir)
    st.status = "paused"
    st.save(ch.state_dir)
    return ch


def test_paused_draft_is_active_labeled_draft(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _park_draft(tmp_path, "pdraft")
    row = next((r for r in m._status_rows(tmp_path) if r[0] == "pdraft"), None)
    assert row is not None
    assert row[1] == "draft"
    assert "pdraft" not in {r[0] for r in m._backlog_rows(tmp_path)}


def test_live_draft_is_active_labeled_running(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Change.create_draft(tmp_path, title="X", change_id="ldraft",
                        base_branch="main")          # fresh ⇒ state status "running"
    row = next((r for r in m._status_rows(tmp_path) if r[0] == "ldraft"), None)
    assert row is not None
    assert row[1] == "running"
    assert "ldraft" not in {r[0] for r in m._backlog_rows(tmp_path)}


def test_idea_stays_in_backlog(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Change.create_draft(tmp_path, title="X", change_id="anidea",
                        base_branch="main", origin="idea", seed_opening="X")
    assert "anidea" not in {r[0] for r in m._status_rows(tmp_path)}
    assert "anidea" in {r[0] for r in m._backlog_rows(tmp_path)}


def test_followup_stays_in_backlog(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Change.create_draft(tmp_path, title="X", change_id="aflwup",
                        base_branch="main", origin="followup", seed_opening="X")
    assert "aflwup" not in {r[0] for r in m._status_rows(tmp_path)}
    assert "aflwup" in {r[0] for r in m._backlog_rows(tmp_path)}


def test_materialized_change_stays_in_active(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Change.create(tmp_path, title="X", change_id="matchg", base_branch="main")
    assert "matchg" in {r[0] for r in m._status_rows(tmp_path)}
    assert "matchg" not in {r[0] for r in m._backlog_rows(tmp_path)}


def test_resume_picker_lists_paused_draft_exactly_once(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _park_draft(tmp_path, "once1")
    ids = [r[0] for r in m._resumable_rows(tmp_path)]
    assert ids.count("once1") == 1
