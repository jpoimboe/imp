from __future__ import annotations

import json
from pathlib import Path

import pytest

from imp.engine import archive
from imp.engine.abandon import archive_conversation
from imp.engine.archive import read as archive_read
from imp.engine.errors import ArchiveMismatch
from imp.engine.quest import Quest
from imp.engine.reincarnation import session_has_resume_state
from imp.engine.state import State
from imp.paths import project_state_dir
from tests.conftest import _seed_control


def test_rehydrate_restores_loadable_draft_from_archive(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create_draft(tmp_path, title="Conv", quest_id="draft", base_branch="main")
    orig_head = ch.orig_base_head
    archive_conversation(ch)  # drops the live home into the archive
    assert not (project_state_dir(tmp_path) / "quests" / "draft").exists()

    quest_dir = archive_read.rehydrate_quest(tmp_path, "draft")
    assert quest_dir == project_state_dir(tmp_path) / "quests" / "draft"

    # The rehydrated home is a faithful, loadable Quest.
    loaded = Quest.load(tmp_path, "draft")
    assert loaded.materialized is False
    assert loaded.orig_base_head == orig_head
    state = State.load(loaded.state_dir)
    assert state.status == "shelved"  # the shelved-conversation resting status
    events = (quest_dir / "events.jsonl").read_bytes()
    assert b"quest_created" in events  # the original conversation events survive


def test_rehydrate_refuses_to_clobber_existing_home(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Quest.create_draft(tmp_path, title="Conv", quest_id="draft", base_branch="main")
    with pytest.raises(FileExistsError):
        archive_read.rehydrate_quest(tmp_path, "draft")


def test_rehydrate_unknown_change_raises(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    with pytest.raises(FileNotFoundError):
        archive_read.rehydrate_quest(tmp_path, "nope")


def _archive_with_steps(control: Path, cid: str, steps: Path, *, resumable: bool) -> None:
    events = control / f"{cid}.jsonl"
    events.write_bytes(b"")
    quest_json = control / f"{cid}-quest.json"
    quest_json.write_text(json.dumps(
        {"id": cid, "title": "C", "base_branch": "main", "materialized": False}))
    state_json = control / f"{cid}-state.json"
    state_json.write_text(json.dumps({"id": cid, "status": "shelved", "step": "s"}))
    archive.archive_quest(
        control, cid, events_path=events,
        quest_json_path=quest_json, state_json_path=state_json,
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

    quest_dir = archive_read.rehydrate_quest(tmp_path, "conv")

    sdir = quest_dir / "steps" / "p" / "s"
    assert (sdir / "transcript.jsonl").read_bytes() == b'{"role":"user"}\n'
    assert (sdir / "claude_session" / "x" / "sess.jsonl").read_bytes() == b"SESS"
    assert (sdir / "codex_rollout" / "sessions" / "rollout-1.jsonl").read_bytes() == b"ROLL"
    # The restored session has resume state.
    assert session_has_resume_state(sdir) is True


def test_rehydrate_non_resumable_restores_no_steps(tmp_path: Path) -> None:
    steps = tmp_path / "src-steps"
    (steps / "p" / "s").mkdir(parents=True)
    (steps / "p" / "s" / "transcript.jsonl").write_bytes(b'{"role":"user"}\n')
    _archive_with_steps(tmp_path, "conv", steps, resumable=False)

    quest_dir = archive_read.rehydrate_quest(tmp_path, "conv")

    assert not (quest_dir / "steps").exists()


def test_rehydrate_read_only_stale_archive_fails_before_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    archive._write_data_version(tmp_path, 11)
    quest_dir = project_state_dir(tmp_path) / "quests" / "legacy"
    monkeypatch.setattr(archive.os, "access", lambda _path, _mode: False)
    calls: list[str] = []

    def unexpected(*_args, **_kwargs):
        calls.append("read")
        raise AssertionError("archive read before the floor check")

    monkeypatch.setattr(archive_read, "read_quest_json", unexpected)
    monkeypatch.setattr(archive_read, "read_state_json", unexpected)
    monkeypatch.setattr(archive_read, "read_events", unexpected)
    monkeypatch.setattr(archive_read, "read_step_blobs", unexpected)

    with pytest.raises(ArchiveMismatch) as excinfo:
        archive_read.rehydrate_quest(tmp_path, "legacy")

    exc = excinfo.value
    assert (
        (exc.floor, exc.stored, exc.known),
        calls,
        quest_dir.exists(),
        archive.data_version_path(tmp_path).read_bytes(),
    ) == (("data", 11, archive._DATA_VERSION), [], False, b"11")
