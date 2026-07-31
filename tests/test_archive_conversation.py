from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path

import pytest

from imp.engine.abandon import archive_conversation
from imp.engine.archive import read as archive_read
from imp.engine.archive import record, records_root
from imp.engine.quest import Quest
from imp.engine.state import State
from imp.paths import project_state_dir
from tests.conftest import _git, _seed_control


def test_archive_conversation_materialized_tears_down_resumable(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="x", quest_id="conv", base_branch="main")

    archive_conversation(ch)

    state = project_state_dir(tmp_path)
    assert not (state / "worktrees" / "conv").exists()                         # worktree gone
    assert _git(tmp_path, "branch", "--list", "imp/conv").stdout.strip() == ""  # branch gone
    assert not (state / "quests" / "conv").exists()                            # live home dropped

    # Archived, resumable, NOT abandoned.
    rec = archive_read.read_quest_json(tmp_path, "conv")
    assert rec is not None
    assert archive_read.status_of(tmp_path, "conv") == "shelved"             # not "abandoned"
    events = archive_read.read_events(tmp_path, "conv")
    assert events is not None and b"conversation_archived" in events
    assert b"quest_abandoned" not in events
    full = record.read_record(records_root(tmp_path), "conv")
    assert full is not None and full["resumable"] is True


def test_archive_conversation_unmaterialized_draft_has_no_worktree(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create_draft(tmp_path, title="x", quest_id="draft", base_branch="main")
    assert not ch.materialized

    archive_conversation(ch)  # must not raise on absent worktree/branch

    assert not (project_state_dir(tmp_path) / "quests" / "draft").exists()
    full = record.read_record(records_root(tmp_path), "draft")
    assert full is not None
    assert full["resumable"] is True
    assert archive_read.status_of(tmp_path, "draft") == "shelved"
    assert full["quest_meta"]["materialized"] is False        # faithful draft state preserved


def test_archive_conversation_captures_steps_subtree(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create_draft(tmp_path, title="x", quest_id="draft", base_branch="main")
    tdir = ch.quest_dir / "steps" / "p" / "s"
    tdir.mkdir(parents=True)
    (tdir / "transcript.jsonl").write_bytes(b'{"role":"assistant"}\n')

    archive_conversation(ch)

    rec = record.read_record(records_root(tmp_path), "draft")
    assert rec is not None
    assert rec["step_blobs"].get("p/s/transcript.jsonl") is not None


@pytest.mark.timeout(240)  # ~13 git subprocesses incl. worktree add/remove starve under -n auto
def test_archive_conversation_arms_resume_pending(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="x", quest_id="armed", base_branch="main")
    initial = State.load(ch.state_dir)
    step = initial.step
    assert step is not None                                    # entry step exists
    assert initial.resume_pending is None                      # fresh: not yet armed

    archive_conversation(ch)

    raw = archive_read.read_state_json(tmp_path, "armed")
    assert raw is not None
    persisted = json.loads(raw)
    assert persisted["resume_pending"] == step                 # armed to the step on leave
    assert persisted["resume_should_continue"] is False               # clean yield re-solicits
    assert "interrupt_pending" not in persisted


def test_archive_conversation_reaps_per_quest_tmp_dir(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="x", quest_id="conv", base_branch="main")
    ch.tmp_dir.mkdir(parents=True, exist_ok=True)
    (ch.tmp_dir / "session.sock").write_bytes(b"")

    archive_conversation(ch)

    assert not ch.tmp_dir.exists()                   # per-quest scratch gone


@pytest.mark.skipif(not os.access("/var/tmp", os.W_OK), reason="requires writable /var/tmp")
def test_archive_conversation_reaps_per_quest_var_tmp_dir(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qid = f"vt{uuid.uuid4().hex[:8]}"
    ch = Quest.create(tmp_path, title="x", quest_id=qid, base_branch="main")
    ch.var_tmp_dir.mkdir(parents=True, exist_ok=True)
    (ch.var_tmp_dir / "scratch").write_text("x")
    try:
        archive_conversation(ch)
        assert not ch.var_tmp_dir.exists()
    finally:
        shutil.rmtree(ch.var_tmp_dir, ignore_errors=True)
