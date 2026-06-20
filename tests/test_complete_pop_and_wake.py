
import shutil
from pathlib import Path

import anyio

import imp.remote.session.attach as attach_mod
from imp.engine import merge_queue as mq
from imp.engine.change import Change
from imp.engine.executor import RunResult, _complete, _route_merge
from imp.engine.loader import load_workflow
from imp.engine.state import JsonlEventLog, State
from tests.conftest import _git, _seed_control


def test_complete_after_merge_pops_self_and_wakes_next(tmp_path, monkeypatch) -> None:
    _seed_control(tmp_path)
    a = Change.create(tmp_path, title="a", change_id="aaa", base_branch="main")
    Change.create(tmp_path, title="b", change_id="bbb", base_branch="main")
    (a.project_root / "f.txt").write_text("hi")
    _git(a.project_root, "add", "-A")
    _git(a.project_root, "commit", "-qm", "a")
    mq.enqueue_if_absent(tmp_path, "aaa", "main")        # head
    mq.enqueue_if_absent(tmp_path, "bbb", "main")        # next

    woken: list = []
    monkeypatch.setattr(attach_mod, "spawn_session_daemon",
                        lambda change, **kw: woken.append((change.id, kw.get("resume"))))

    state = State.load(a.state_dir)
    state.merge_commit = "deadbeef"                      # mark the merge already durable
    log = JsonlEventLog(a.events_path)
    _complete(a, state, log)

    assert all(e["change_id"] != "aaa" for e in mq.read(tmp_path))   # head popped
    assert woken == [("bbb", True)]                                  # next woken


def test_complete_without_merge_does_not_touch_queue(tmp_path, monkeypatch) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="x", change_id="feat", base_branch="main")
    mq.enqueue_if_absent(tmp_path, "feat", "main")
    woken: list = []
    monkeypatch.setattr(attach_mod, "spawn_session_daemon", lambda *a, **k: woken.append(1))
    state = State.load(ch.state_dir)                     # merge_commit is None
    _complete(ch, state, JsonlEventLog(ch.events_path))
    assert [e["change_id"] for e in mq.read(tmp_path)] == ["feat"]   # untouched
    assert woken == []


def _merge_step():
    wf = load_workflow(
        Path(__file__).resolve().parents[1] / "src" / "imp" / "assets" / "workflows" / "imp")
    for s in wf.flat_steps():
        if s.name == "merge-to-base":
            return s
    raise AssertionError("merge-to-base step not found in default workflow")


def test_complete_finalizes_even_when_next_head_vanishes(tmp_path, monkeypatch) -> None:
    _seed_control(tmp_path)
    a = Change.create(tmp_path, title="a", change_id="aaa", base_branch="main")
    Change.create(tmp_path, title="b", change_id="bbb", base_branch="main")
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
    anyio.run(_route_merge, _merge_step(), a, state, log)   # perform the real merge
    assert state.merge_commit is not None                   # finalize path will run

    # Next head's records vanish in the pop->spawn window.
    shutil.rmtree(tmp_path / ".imp" / "changes" / "bbb")

    result = _complete(a, state, log)                       # must NOT raise

    assert result == RunResult.COMPLETED
    assert not (tmp_path / ".imp" / "worktrees" / "aaa").exists()      # worktree removed
    from imp.engine import archive
    assert archive.records_root(tmp_path).joinpath("aaa.json").is_file()  # archived
    assert spawned == []                                    # vanished next head never spawned
