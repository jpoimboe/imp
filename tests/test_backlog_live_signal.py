from __future__ import annotations

import os
from pathlib import Path

import imp.cli.main as m
from imp.engine.change import Change
from tests.conftest import _seed_control


def _idea_seed(tmp_path: Path, cid: str) -> Change:
    return Change.create_draft(tmp_path, title="Idea", change_id=cid,
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
    (ch.change_dir / "lock").write_text(str(os.getpid()))   # live holder pid
    assert m._unstarted_backlog_meta(tmp_path, "2026-06-20-lock01") is None


def test_unstarted_backlog_meta_none_when_conversed(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = _idea_seed(tmp_path, "2026-06-20-conv01")
    _converse(ch, "actually go with postgres")
    assert m._unstarted_backlog_meta(tmp_path, "2026-06-20-conv01") is None


def test_unstarted_backlog_meta_none_when_draft_origin(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Change.create_draft(tmp_path, title="Draft", change_id="2026-06-20-drft01",
                        base_branch="main", origin="draft", seed_opening="Draft")
    assert m._unstarted_backlog_meta(tmp_path, "2026-06-20-drft01") is None
