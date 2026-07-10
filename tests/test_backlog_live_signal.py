from __future__ import annotations

import os
from pathlib import Path

import imp.cli.main as m
from imp.engine.change import Change
from tests.conftest import _seed_control


def _idea_seed(tmp_path: Path, cid: str) -> Change:
    return Change.create_draft(tmp_path, title="Idea", quest_id=cid,
                               base_branch="main", origin="idea",
                               seed_opening="Idea")


def _converse(ch: Change, text: str) -> None:
    ch.event_log().append(
        agent_id=None, type="user_message",
        payload={"role": "user", "content": [{"type": "text", "text": text}]})


def test_unstarted_backlog_meta_returns_meta_for_idle_seed(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _idea_seed(tmp_path, "2026-06-20-idle01")
    meta = m._unstarted_backlog_meta(tmp_path, "2026-06-20-idle01")
    assert meta is not None
    assert meta["origin"] == "idea"


def test_unstarted_backlog_meta_none_when_lock_live(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = _idea_seed(tmp_path, "2026-06-20-lock01")
    (ch.quest_dir / "lock").write_text(str(os.getpid()))   # live holder pid
    assert m._unstarted_backlog_meta(tmp_path, "2026-06-20-lock01") is None


def test_unstarted_backlog_meta_none_when_conversed(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = _idea_seed(tmp_path, "2026-06-20-conv01")
    _converse(ch, "actually go with postgres")
    assert m._unstarted_backlog_meta(tmp_path, "2026-06-20-conv01") is None


def test_unstarted_backlog_meta_none_when_draft_origin(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Change.create_draft(tmp_path, title="Draft", quest_id="2026-06-20-drft01",
                        base_branch="main", origin="draft", seed_opening="Draft")
    assert m._unstarted_backlog_meta(tmp_path, "2026-06-20-drft01") is None


def test_untouched_seed_stays_in_backlog(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _idea_seed(tmp_path, "2026-06-20-bkg01")
    assert "2026-06-20-bkg01" not in {r[0] for r in m._status_rows(tmp_path)}
    assert "2026-06-20-bkg01" in {r[0] for r in m._backlog_rows(tmp_path)}


def test_lock_live_seed_leaves_backlog_and_shows_running(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = _idea_seed(tmp_path, "2026-06-20-run01")
    (ch.quest_dir / "lock").write_text(str(os.getpid()))   # live holder pid
    status_rows = {r[0]: r for r in m._status_rows(tmp_path)}
    assert "2026-06-20-run01" in status_rows
    assert status_rows["2026-06-20-run01"][1] == "running"
    assert "2026-06-20-run01" not in {r[0] for r in m._backlog_rows(tmp_path)}


def test_conversed_unmaterialized_seed_is_active_draft(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = _idea_seed(tmp_path, "2026-06-20-drf01")
    _converse(ch, "let's start with the parser")
    status_rows = {r[0]: r for r in m._status_rows(tmp_path)}
    assert "2026-06-20-drf01" in status_rows
    assert status_rows["2026-06-20-drf01"][1] == "draft"
    assert "2026-06-20-drf01" not in {r[0] for r in m._backlog_rows(tmp_path)}
