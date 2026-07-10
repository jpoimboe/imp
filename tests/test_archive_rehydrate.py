from __future__ import annotations

import json
from pathlib import Path

import pytest

from imp.engine import archive
from imp.engine.abandon import archive_conversation
from imp.engine.archive import read as archive_read
from imp.engine.change import Change
from imp.engine.executor import _resume_replays_transcript
from imp.engine.state import State
from tests.conftest import _seed_control


def test_rehydrate_restores_loadable_draft_from_archive(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create_draft(tmp_path, title="Conv", quest_id="draft", base_branch="main")
    orig_head = ch.orig_base_head
    archive_conversation(ch)  # drops the live home into the archive
    assert not (tmp_path / ".imp" / "changes" / "draft").exists()

    quest_dir = archive_read.rehydrate_change(tmp_path, "draft")
    assert quest_dir == tmp_path / ".imp" / "changes" / "draft"

    # The rehydrated home is a faithful, loadable Change.
    loaded = Change.load(tmp_path, "draft")
    assert loaded.materialized is False
    assert loaded.orig_base_head == orig_head
    state = State.load(loaded.state_dir)
    assert state.status == "paused"  # the left-conversation resting status
    events = (quest_dir / "events.jsonl").read_bytes()
    assert b"change_created" in events  # the original conversation events survive


def test_rehydrate_refuses_to_clobber_existing_home(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Change.create_draft(tmp_path, title="Conv", quest_id="draft", base_branch="main")
    with pytest.raises(FileExistsError):
        archive_read.rehydrate_change(tmp_path, "draft")


def test_rehydrate_unknown_change_raises(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    with pytest.raises(FileNotFoundError):
        archive_read.rehydrate_change(tmp_path, "nope")


def _archive_with_steps(control: Path, cid: str, steps: Path, *, resumable: bool) -> None:
    events = control / f"{cid}.jsonl"
    events.write_bytes(b"")
    change_json = control / f"{cid}-change.json"
    change_json.write_text(json.dumps(
        {"id": cid, "title": "C", "base_branch": "main", "materialized": False}))
    state_json = control / f"{cid}-state.json"
    state_json.write_text(json.dumps({"id": cid, "status": "paused", "step": "s"}))
    archive.archive_change(
        control, cid, events_path=events,
        change_json_path=change_json, state_json_path=state_json,
        steps_dir=steps, resumable=resumable)


def test_rehydrate_restores_steps_subtree_byte_for_byte(tmp_path: Path) -> None:
    steps = tmp_path / "src-steps"
    (steps / "p" / "s").mkdir(parents=True)
    (steps / "p" / "s" / "transcript.jsonl").write_bytes(b'{"role":"user"}\n')
    (steps / "p" / "s" / "claude_session" / "x").mkdir(parents=True)
    (steps / "p" / "s" / "claude_session" / "x" / "sess.jsonl").write_bytes(b"SESS")
    (steps / "p" / "s" / "codex_rollout" / "sessions").mkdir(parents=True)
    (steps / "p" / "s" / "codex_rollout" / "sessions" / "rollout-1.jsonl").write_bytes(b"ROLL")
    _archive_with_steps(tmp_path, "conv", steps, resumable=True)

    quest_dir = archive_read.rehydrate_change(tmp_path, "conv")

    sdir = quest_dir / "steps" / "p" / "s"
    assert (sdir / "transcript.jsonl").read_bytes() == b'{"role":"user"}\n'
    assert (sdir / "claude_session" / "x" / "sess.jsonl").read_bytes() == b"SESS"
    assert (sdir / "codex_rollout" / "sessions" / "rollout-1.jsonl").read_bytes() == b"ROLL"
    # The restored transcript makes the executor's replay decision fire.
    assert _resume_replays_transcript(sdir) is True


def test_rehydrate_non_resumable_restores_no_steps(tmp_path: Path) -> None:
    steps = tmp_path / "src-steps"
    (steps / "p" / "s").mkdir(parents=True)
    (steps / "p" / "s" / "transcript.jsonl").write_bytes(b'{"role":"user"}\n')
    _archive_with_steps(tmp_path, "conv", steps, resumable=False)

    quest_dir = archive_read.rehydrate_change(tmp_path, "conv")

    assert not (quest_dir / "steps").exists()
