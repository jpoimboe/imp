
import os
import shutil
import uuid
from pathlib import Path

import anyio
import pytest

import imp.remote.session.attach as attach_mod
from imp.engine import merge_queue as mq
from imp.engine.executor import RunResult, _complete, _route_merge_and_exit_worktree
from imp.engine.loader import load_workflow
from imp.engine.quest import Quest
from imp.engine.state import JsonlEventLog, State
from tests.conftest import _git, _seed_control


def test_complete_after_merge_pops_self_and_wakes_next(tmp_path, monkeypatch) -> None:
    _seed_control(tmp_path)
    a = Quest.create(tmp_path, title="a", quest_id="aaa", base_branch="main")
    Quest.create(tmp_path, title="b", quest_id="bbb", base_branch="main")
    (a.project_root / "f.txt").write_text("hi")
    _git(a.project_root, "add", "-A")
    _git(a.project_root, "commit", "-qm", "a")
    mq.enqueue_if_absent(tmp_path, "aaa", "main")        # head
    mq.enqueue_if_absent(tmp_path, "bbb", "main")        # next

    woken: list = []
    monkeypatch.setattr(attach_mod, "spawn_session_daemon",
                        lambda quest, **kw: woken.append((quest.id, kw.get("resume"))))

    state = State.load(a.state_dir)
    state.merge_commit = "deadbeef"                      # mark the merge already durable
    log = JsonlEventLog(a.events_path)
    _complete(a, state, log)

    assert all(e["quest_id"] != "aaa" for e in mq.read(tmp_path))   # head popped
    assert woken == [("bbb", True)]                                  # next woken


def _merge_step():
    wf = load_workflow(
        Path(__file__).resolve().parents[1] / "src" / "imp" / "assets" / "workflows" / "imp")
    for s in wf.flat_steps():
        if s.name == "merge-to-base":
            return s
    raise AssertionError("merge-to-base step not found in default workflow")


def test_complete_finalizes_even_when_next_head_vanishes(tmp_path, monkeypatch) -> None:
    _seed_control(tmp_path)
    a = Quest.create(tmp_path, title="a", quest_id="aaa", base_branch="main")
    Quest.create(tmp_path, title="b", quest_id="bbb", base_branch="main")
    (a.project_root / "f.txt").write_text("hi")
    _git(a.project_root, "add", "-A")
    _git(a.project_root, "commit", "-qm", "a")
    mq.enqueue_if_absent(tmp_path, "aaa", "main")        # head
    mq.enqueue_if_absent(tmp_path, "bbb", "main")        # next

    spawned: list = []
    monkeypatch.setattr(attach_mod, "spawn_session_daemon",
                        lambda *a, **k: spawned.append(1))

    state = State.load(a.state_dir)
    log = JsonlEventLog(a.events_path)
    # perform the real merge
    anyio.run(_route_merge_and_exit_worktree, _merge_step(), a, state, log)
    assert state.merge_commit is not None                   # finalize path will run

    # Next head's records vanish in the pop->spawn window.
    shutil.rmtree(tmp_path / ".imp" / "quests" / "bbb")

    result = _complete(a, state, log)                       # must NOT raise

    assert result == RunResult.COMPLETED
    assert not (tmp_path / ".imp" / "worktrees" / "aaa").exists()
    from imp.engine import archive
    assert archive.records_root(tmp_path).joinpath("aaa.json").is_file()  # archived
    assert spawned == []                                    # vanished next head never spawned


def test_complete_without_merge_releases_queue_slot(tmp_path, monkeypatch) -> None:
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    mq.enqueue_if_absent(tmp_path, "feat", "main")
    monkeypatch.setattr(attach_mod, "spawn_session_daemon", lambda *a, **k: None)
    state = State.load(ch.state_dir)                     # merge_commit is None
    _complete(ch, state, JsonlEventLog(ch.events_path))
    assert mq.read(tmp_path) == []                       # lane released on completion


def test_complete_without_merge_emits_workflow_completed(tmp_path, monkeypatch) -> None:
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    mq.enqueue_if_absent(tmp_path, "feat", "main")
    monkeypatch.setattr(attach_mod, "spawn_session_daemon", lambda *a, **k: None)
    state = State.load(ch.state_dir)                     # merge_commit is None
    log = JsonlEventLog(ch.events_path)
    result = _complete(ch, state, log)
    assert result == RunResult.COMPLETED
    types = [e.get("type") for e in log.read()]
    assert "workflow_completed" in types                 # generic terminal event emitted
    # the merge-only event is NOT emitted on the no-merge path
    assert "quest_merged" not in types
    completed = [e for e in log.read() if e.get("type") == "workflow_completed"]
    assert completed and completed[0].get("payload") == {}


def test_complete_without_merge_reaps_quest_tmp_dir(tmp_path, monkeypatch) -> None:
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    mq.enqueue_if_absent(tmp_path, "feat", "main")
    monkeypatch.setattr(attach_mod, "spawn_session_daemon", lambda *a, **k: None)
    ch.tmp_dir.mkdir(parents=True, exist_ok=True)
    (ch.tmp_dir / "session.sock").write_bytes(b"")   # scratch that must be reaped
    state = State.load(ch.state_dir)                 # merge_commit is None → no-merge branch
    _complete(ch, state, JsonlEventLog(ch.events_path))
    assert not ch.tmp_dir.exists()                   # per-quest scratch gone


def test_complete_after_merge_reaps_quest_tmp_dir(tmp_path, monkeypatch) -> None:
    _seed_control(tmp_path)
    a = Quest.create(tmp_path, title="a", quest_id="aaa", base_branch="main")
    (a.project_root / "f.txt").write_text("hi")
    _git(a.project_root, "add", "-A")
    _git(a.project_root, "commit", "-qm", "a")
    mq.enqueue_if_absent(tmp_path, "aaa", "main")
    monkeypatch.setattr(attach_mod, "spawn_session_daemon", lambda *a, **k: None)
    a.tmp_dir.mkdir(parents=True, exist_ok=True)
    (a.tmp_dir / "session.sock").write_bytes(b"")
    state = State.load(a.state_dir)
    state.merge_commit = "deadbeef"  # mark the merge already durable → finalize branch
    _complete(a, state, JsonlEventLog(a.events_path))
    assert not a.tmp_dir.exists()                    # per-quest scratch gone on the merge path too


@pytest.mark.skipif(not os.access("/var/tmp", os.W_OK), reason="requires writable /var/tmp")
def test_complete_without_merge_reaps_per_quest_var_tmp_dir(tmp_path, monkeypatch) -> None:
    _seed_control(tmp_path)
    qid = f"vt{uuid.uuid4().hex[:8]}"
    ch = Quest.create(tmp_path, title="x", quest_id=qid, base_branch="main")
    mq.enqueue_if_absent(tmp_path, qid, "main")
    monkeypatch.setattr(attach_mod, "spawn_session_daemon", lambda *a, **k: None)
    ch.var_tmp_dir.mkdir(parents=True, exist_ok=True)
    (ch.var_tmp_dir / "scratch").write_text("x")
    try:
        state = State.load(ch.state_dir)     # merge_commit is None → no-merge branch
        _complete(ch, state, JsonlEventLog(ch.events_path))
        assert not ch.var_tmp_dir.exists()
    finally:
        shutil.rmtree(ch.var_tmp_dir, ignore_errors=True)


@pytest.mark.skipif(not os.access("/var/tmp", os.W_OK), reason="requires writable /var/tmp")
def test_complete_after_merge_reaps_per_quest_var_tmp_dir(tmp_path, monkeypatch) -> None:
    _seed_control(tmp_path)
    qid = f"vt{uuid.uuid4().hex[:8]}"
    a = Quest.create(tmp_path, title="a", quest_id=qid, base_branch="main")
    (a.project_root / "f.txt").write_text("hi")
    _git(a.project_root, "add", "-A")
    _git(a.project_root, "commit", "-qm", "a")
    mq.enqueue_if_absent(tmp_path, qid, "main")
    monkeypatch.setattr(attach_mod, "spawn_session_daemon", lambda *a, **k: None)
    a.var_tmp_dir.mkdir(parents=True, exist_ok=True)
    (a.var_tmp_dir / "scratch").write_text("x")
    try:
        state = State.load(a.state_dir)
        state.merge_commit = "deadbeef"      # mark merge durable → finalize branch
        _complete(a, state, JsonlEventLog(a.events_path))
        assert not a.var_tmp_dir.exists()
    finally:
        shutil.rmtree(a.var_tmp_dir, ignore_errors=True)
