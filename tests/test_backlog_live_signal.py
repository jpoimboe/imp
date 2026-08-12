from __future__ import annotations

import json
import os
from pathlib import Path

import imp.cli.main as m
import imp.cli.store as s
from imp.engine.quest import LAYOUT_VERSION, Quest
from tests.conftest import _seed_control


def _idea_seed(tmp_path: Path, cid: str) -> Quest:
    return Quest.create_draft(tmp_path, title="Idea", quest_id=cid,
                               base_branch="main", origin="idea",
                               seed_opening="Idea")


def _converse(ch: Quest, text: str) -> None:
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
    Quest.create_draft(tmp_path, title="Draft", quest_id="2026-06-20-drft01",
                        base_branch="main", origin="draft", seed_opening="Draft")
    assert m._unstarted_backlog_meta(tmp_path, "2026-06-20-drft01") is None


def test_unstarted_backlog_meta_returns_meta_when_quest_load_would_fail(
        tmp_path: Path) -> None:
    # A layout stamped newer than this engine makes Quest.load raise LayoutMismatch.
    # The scan keeps the raw quest.json, so the detail card now agrees with the
    # overview's Backlog zone instead of falling through to the active renderer.
    _seed_control(tmp_path)
    q = _idea_seed(tmp_path, "2026-06-20-mism01")
    meta_path = q.quest_dir / "quest.json"
    stamped = json.loads(meta_path.read_text())
    stamped["layout_version"] = LAYOUT_VERSION + 100
    meta_path.write_text(json.dumps(stamped))
    meta = m._unstarted_backlog_meta(tmp_path, "2026-06-20-mism01")
    assert meta is not None
    assert meta["origin"] == "idea"
    assert "2026-06-20-mism01" in {r[0] for r in s._backlog_rows(tmp_path)}


def test_unstarted_backlog_meta_none_when_only_user_message_is_empty(
        tmp_path: Path) -> None:
    # Any user_message event counts as conversed, empty text included — the
    # overview's rule, now the card's too.
    _seed_control(tmp_path)
    ch = _idea_seed(tmp_path, "2026-06-20-emty01")
    _converse(ch, "")
    assert m._unstarted_backlog_meta(tmp_path, "2026-06-20-emty01") is None
    assert "2026-06-20-emty01" not in {r[0] for r in s._backlog_rows(tmp_path)}


def test_untouched_seed_stays_in_backlog(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _idea_seed(tmp_path, "2026-06-20-bkg01")
    assert "2026-06-20-bkg01" not in {r[0] for r in s._status_rows(tmp_path)}
    assert "2026-06-20-bkg01" in {r[0] for r in s._backlog_rows(tmp_path)}


def test_lock_live_seed_leaves_backlog_and_shows_running(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = _idea_seed(tmp_path, "2026-06-20-run01")
    (ch.quest_dir / "lock").write_text(str(os.getpid()))   # live holder pid
    status_rows = {r[0]: r for r in s._status_rows(tmp_path)}
    assert "2026-06-20-run01" in status_rows
    assert status_rows["2026-06-20-run01"][1] == "running"
    assert "2026-06-20-run01" not in {r[0] for r in s._backlog_rows(tmp_path)}


def test_conversed_unmaterialized_seed_is_active_new(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = _idea_seed(tmp_path, "2026-06-20-drf01")
    _converse(ch, "let's start with the parser")
    status_rows = {r[0]: r for r in s._status_rows(tmp_path)}
    assert "2026-06-20-drf01" in status_rows
    assert status_rows["2026-06-20-drf01"][1] == "new"
    assert "2026-06-20-drf01" not in {r[0] for r in s._backlog_rows(tmp_path)}
