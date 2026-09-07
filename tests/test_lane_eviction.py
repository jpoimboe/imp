from pathlib import Path

import anyio

import imp.remote.session.attach as attach_mod
from imp.engine import merge_queue as mq
from imp.engine import worktree
from imp.engine.executor import (
    RunResult,
    _evict_if_holding,
    _worktree_head,
    run_quest,
)
from imp.engine.quest import Quest
from imp.engine.state import JsonlEventLog, LaneRestore, State
from imp.paths import project_state_dir
from tests.conftest import _branch_with_commit, _git, _seed_control


def test_evict_releases_lane_and_rewinds(tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    a = Quest.create(tmp_path, title="a", quest_id="aaa", base_branch="main")
    Quest.create(tmp_path, title="b", quest_id="bbb", base_branch="main")
    mq.enqueue_if_absent(tmp_path, "aaa", "main")   # head
    mq.enqueue_if_absent(tmp_path, "bbb", "main")   # next
    # eviction pops the head and wakes the next head; stub the real daemon spawn.
    monkeypatch.setattr(attach_mod, "spawn_session_daemon", lambda *a, **k: None)
    state = State.load(a.state_dir)
    state.held_lane = "main"
    state.held_acquire_step = "lane"
    state.step = "touch-base"            # somewhere inside the held region
    state.resume_pending = "touch-base"
    log = JsonlEventLog(a.events_path)

    evicted = _evict_if_holding(a, state, log)

    assert evicted is True
    assert all(e["quest_id"] != "aaa" for e in mq.read(tmp_path))   # lane released
    assert state.step == "lane"          # rewound to the acquiring step
    assert state.resume_pending is None  # not an in-place resume
    assert state.held_lane is None
    assert state.held_acquire_step is None


def test_evict_clears_resume_should_continue_pair(tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    a = Quest.create(tmp_path, title="a", quest_id="aaa", base_branch="main")
    Quest.create(tmp_path, title="b", quest_id="bbb", base_branch="main")
    mq.enqueue_if_absent(tmp_path, "aaa", "main")   # head
    mq.enqueue_if_absent(tmp_path, "bbb", "main")   # next
    monkeypatch.setattr(attach_mod, "spawn_session_daemon", lambda *a, **k: None)
    state = State.load(a.state_dir)
    state.held_lane = "main"
    state.held_acquire_step = "lane"
    state.step = "touch-base"                 # somewhere inside the held region
    state.arm_step_resume(should_continue=True)    # a genuine armed continue resume
    assert state.resume_pending == "touch-base" and state.resume_should_continue is True

    evicted = _evict_if_holding(a, state, JsonlEventLog(a.events_path))

    assert evicted is True
    assert state.resume_pending is None   # pair cleared together — no tear
    assert state.resume_should_continue is False


def test_evict_noop_when_not_holding(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    state = State.load(ch.state_dir)      # held_lane is None
    assert _evict_if_holding(ch, state, JsonlEventLog(ch.events_path)) is False


def _commit_workflow(root: Path, message: str) -> None:
    """Disable the sandbox gate (deterministic across environments) and commit."""
    cfg_path = project_state_dir(root) / "config.yaml"
    cfg_path.write_text(cfg_path.read_text() + "sandbox:\n  enabled: false\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", message)


def _write_serialize_workflow(root: Path, touch_cmd: str) -> None:
    wf = root / "imp" / "workflows" / "imp" / "workflow.yaml"
    wf.write_text(
        "name: t\ndescription: \"\"\n"
        "phases:\n"
        "  - name: serialize\n"
        "    steps:\n"
        "      - name: lane\n"
        "        acquire: merge-lane\n"
        "        rules:\n"
        "          - {else: true, continue: true}\n"
        "      - name: touch-base\n"
        f"        command: \"{touch_cmd}\"\n"
        "        rules:\n"
        "          - {if: {exit: 0}, continue: true}\n"
        "          - {else: true, pause: true}\n"
    )
    _commit_workflow(root, "serialize workflow")


def test_pause_in_held_region_evicts_and_rewinds(tmp_path: Path, provider_registry) -> None:
    from tests.test_executor_rename import _FakeProvider
    provider_registry("anthropic", lambda: True, lambda model: _FakeProvider(model))
    _seed_control(tmp_path)
    _write_serialize_workflow(tmp_path, "false")   # touch-base fails -> pause
    ch = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    result = anyio.run(run_quest, ch)
    assert result is RunResult.PAUSED
    reloaded = State.load(ch.state_dir)
    assert reloaded.step == "lane"                 # resume cursor rewound to acquire
    assert reloaded.held_lane is None              # lane released in-process
    assert mq.read(tmp_path) == []                 # the sole holder was popped


def test_crash_resume_with_held_mark_evicts_at_entry(tmp_path: Path, provider_registry) -> None:
    from tests.test_executor_rename import _FakeProvider
    provider_registry("anthropic", lambda: True, lambda model: _FakeProvider(model))
    _seed_control(tmp_path)
    _write_serialize_workflow(tmp_path, "true")    # touch-base succeeds
    ch = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    # Simulate a crash WHILE holding: mark set, cursor mid-held-region, still running.
    mq.enqueue_if_absent(tmp_path, "feat", "main")
    state = State.load(ch.state_dir)
    state.held_lane = "main"
    state.held_acquire_step = "lane"
    state.step = "touch-base"
    state.status = "running"
    state.save(ch.state_dir)

    anyio.run(run_quest, ch)
    from imp.engine.archive import read as archive_read
    archive_read.rehydrate_quest(ch.control_root, ch.id)  # home reaped on done
    types_seen = [e.get("type") for e in JsonlEventLog(ch.events_path).read()]
    assert "lane_evicted" in types_seen            # entry eviction fired on resume
    # After eviction the change re-acquires, runs touch-base, and completes with
    # no merge — the no-merge completion path must release the lane (AC2), so the
    # queue is empty rather than leaking the re-acquired entry.
    assert State.load(ch.state_dir).status == "done"
    assert mq.read(tmp_path) == []


def _hold(state: State, *, step: str) -> None:
    state.held_lane = "main"
    state.held_acquire_step = "lane"
    state.step = step


def test_evict_records_restore_point_with_both_heads(
        tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    a = Quest.create(tmp_path, title="a", quest_id="aaa", base_branch="main")
    Quest.create(tmp_path, title="b", quest_id="bbb", base_branch="main")
    mq.enqueue_if_absent(tmp_path, "aaa", "main")
    mq.enqueue_if_absent(tmp_path, "bbb", "main")
    monkeypatch.setattr(attach_mod, "spawn_session_daemon", lambda *a, **k: None)
    state = State.load(a.state_dir)
    _hold(state, step="summarizer")
    base_head = worktree.head_of(tmp_path, "main")
    quest_head = _worktree_head(a)
    assert base_head and quest_head

    assert _evict_if_holding(a, state, JsonlEventLog(a.events_path)) is True

    assert state.lane_restore == LaneRestore(
        step="summarizer", base_head=base_head, quest_head=quest_head)
    state.save(a.state_dir)
    assert State.load(a.state_dir).lane_restore == state.lane_restore


def test_evict_at_acquire_step_records_no_restore_point(
        tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    a = Quest.create(tmp_path, title="a", quest_id="aaa", base_branch="main")
    mq.enqueue_if_absent(tmp_path, "aaa", "main")
    monkeypatch.setattr(attach_mod, "spawn_session_daemon", lambda *a, **k: None)
    state = State.load(a.state_dir)
    _hold(state, step="lane")            # cursor already AT the acquiring step

    assert _evict_if_holding(a, state, JsonlEventLog(a.events_path)) is True

    assert state.lane_restore is None


def test_evict_records_nothing_when_base_ref_is_unresolvable(
        tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    _branch_with_commit(tmp_path, "base")     # control root stays on main
    a = Quest.create(tmp_path, title="a", quest_id="aaa", base_branch="base")
    mq.enqueue_if_absent(tmp_path, "aaa", "base")
    monkeypatch.setattr(attach_mod, "spawn_session_daemon", lambda *a, **k: None)
    state = State.load(a.state_dir)
    state.held_lane = "base"
    state.held_acquire_step = "lane"
    state.step = "summarizer"
    _git(tmp_path, "branch", "-D", "base")    # the base ref no longer resolves

    # No RuntimeError escapes, and the lane is still popped and the cursor rewound.
    assert _evict_if_holding(a, state, JsonlEventLog(a.events_path)) is True

    assert state.lane_restore is None
    assert state.step == "lane"
    assert all(e["quest_id"] != "aaa" for e in mq.read(tmp_path))


def test_evict_records_nothing_when_worktree_head_is_empty(
        tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    a = Quest.create(tmp_path, title="a", quest_id="aaa", base_branch="main")
    mq.enqueue_if_absent(tmp_path, "aaa", "main")
    monkeypatch.setattr(attach_mod, "spawn_session_daemon", lambda *a, **k: None)
    monkeypatch.setattr(
        "imp.engine.executor._worktree_head", lambda _quest: "")   # unborn HEAD
    state = State.load(a.state_dir)
    _hold(state, step="summarizer")

    assert _evict_if_holding(a, state, JsonlEventLog(a.events_path)) is True

    assert state.lane_restore is None


def test_evict_records_base_head_from_before_the_pop(
        tmp_path: Path, monkeypatch) -> None:
    """The pop wakes the next quest; a base advanced by that wake must never be
    recorded as the pre-pause base."""
    _seed_control(tmp_path)
    a = Quest.create(tmp_path, title="a", quest_id="aaa", base_branch="main")
    mq.enqueue_if_absent(tmp_path, "aaa", "main")
    pre_pop = worktree.head_of(tmp_path, "main")

    def _advancing_pop(control_root, quest_id, lane):
        (tmp_path / "woken.txt").write_text("x\n")
        _git(tmp_path, "add", "-A")
        _git(tmp_path, "commit", "-qm", "the woken quest merged")

    monkeypatch.setattr(mq, "pop_and_wake", _advancing_pop)
    state = State.load(a.state_dir)
    _hold(state, step="summarizer")

    assert _evict_if_holding(a, state, JsonlEventLog(a.events_path)) is True

    post_pop = worktree.head_of(tmp_path, "main")
    assert post_pop != pre_pop                     # the stub really moved main
    assert state.lane_restore is not None
    assert state.lane_restore.base_head == pre_pop


def test_entry_guard_eviction_clears_restore_and_replays_the_phase(
        tmp_path: Path, provider_registry) -> None:
    """A hold mark surviving from a run that died holding: the entry guard must
    neither record nor honor a restore point — the phase replays from `lane`."""
    from tests.test_executor_rename import _FakeProvider
    provider_registry("anthropic", lambda: True, lambda model: _FakeProvider(model))
    _seed_control(tmp_path)
    _write_serialize_workflow(tmp_path, "true")     # touch-base succeeds
    ch = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    mq.enqueue_if_absent(tmp_path, "feat", "main")
    state = State.load(ch.state_dir)
    state.held_lane = "main"
    state.held_acquire_step = "lane"
    state.step = "touch-base"
    state.status = "running"
    # A restore point left on disk by the crash, with a fingerprint that still
    # matches — only the entry guard's clear stops it from firing.
    state.lane_restore = LaneRestore(
        step="touch-base",
        base_head=worktree.head_of(tmp_path, "main"),
        quest_head=_worktree_head(ch))
    state.save(ch.state_dir)

    anyio.run(run_quest, ch)

    from imp.engine.archive import read as archive_read
    archive_read.rehydrate_quest(ch.control_root, ch.id)
    events = list(JsonlEventLog(ch.events_path).read())
    types_seen = [e.get("type") for e in events]
    assert "lane_evicted" in types_seen              # entry eviction fired
    assert "lane_restored" not in types_seen         # …and no restore was honored
    started = [(e.get("payload") or {}).get("step")
               for e in events if e.get("type") == "step_started"]
    assert started == ["lane", "touch-base"]         # replayed from the acquire step
    assert State.load(ch.state_dir).lane_restore is None


# `flip` fails on its first run and passes on every later one: the sentinel lives in
# the quest home ($IMP_QUEST_DIR is injected by engine_env), which survives the pause.
_FLIP_ONCE = 'test -f "$IMP_QUEST_DIR/flip" || { : > "$IMP_QUEST_DIR/flip"; exit 1; }'


def _write_restore_workflow(root: Path) -> None:
    """A five-step merge phase: the acquiring step, two steps that must not replay,
    the step that pauses, and a tail step standing in for the merge."""
    wf = root / "imp" / "workflows" / "imp" / "workflow.yaml"
    wf.write_text(
        "name: t\ndescription: \"\"\n"
        "phases:\n"
        "  - name: serialize\n"
        "    steps:\n"
        "      - name: lane\n"
        "        acquire: merge-lane\n"
        "        rules:\n"
        "          - {else: true, continue: true}\n"
        "      - name: alpha\n"
        "        command: \"true\"\n"
        "        rules:\n"
        "          - {else: true, continue: true}\n"
        "      - name: beta\n"
        "        command: \"true\"\n"
        "        rules:\n"
        "          - {else: true, continue: true}\n"
        "      - name: flip\n"
        f"        command: '{_FLIP_ONCE}'\n"
        "        rules:\n"
        "          - {if: {exit: 0}, continue: true}\n"
        "          - {else: true, pause: true}\n"
        "      - name: gamma\n"
        "        command: \"true\"\n"
        "        rules:\n"
        "          - {else: true, continue: true}\n"
    )
    _commit_workflow(root, "restore workflow")


def _steps_started(quest: Quest) -> list[str]:
    from imp.engine.archive import read as archive_read
    archive_read.rehydrate_quest(quest.control_root, quest.id)   # home reaped on done
    return [(e.get("payload") or {}).get("step")
            for e in JsonlEventLog(quest.events_path).read()
            if e.get("type") == "step_started"]


def test_resume_after_pause_restores_without_replaying_the_phase(
        tmp_path: Path, provider_registry) -> None:
    """Every step strictly between the acquiring step and the pause point runs
    exactly once across the pause and the resume."""
    from tests.test_executor_rename import _FakeProvider
    provider_registry("anthropic", lambda: True, lambda model: _FakeProvider(model))
    _seed_control(tmp_path)
    _write_restore_workflow(tmp_path)
    ch = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")

    assert anyio.run(run_quest, ch) is RunResult.PAUSED
    paused = State.load(ch.state_dir)
    assert paused.step == "lane"                 # the coarse rewind still happens
    assert paused.lane_restore is not None
    assert paused.lane_restore.step == "flip"

    assert anyio.run(run_quest, ch) is RunResult.COMPLETED

    started = _steps_started(ch)
    # `lane` re-runs by design (the lane must be re-acquired); nothing else does.
    assert started == ["lane", "alpha", "beta", "flip", "lane", "flip", "gamma"]
    events = list(JsonlEventLog(ch.events_path).read())
    restored = [e["payload"] for e in events if e["type"] == "lane_restored"]
    assert restored == [{"base": "main", "step": "flip"}]
    assert State.load(ch.state_dir).lane_restore is None
    assert mq.read(tmp_path) == []               # lane released at completion


def test_entry_guard_replays_the_whole_phase(tmp_path: Path, provider_registry) -> None:
    """The negative control for the test above: on the crash path the same five-step
    phase replays in full, so the exactly-once counts there are the restore's doing."""
    from tests.test_executor_rename import _FakeProvider
    provider_registry("anthropic", lambda: True, lambda model: _FakeProvider(model))
    _seed_control(tmp_path)
    _write_restore_workflow(tmp_path)
    ch = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    (ch.quest_dir / "flip").write_text("")       # `flip` passes on its first run
    mq.enqueue_if_absent(tmp_path, "feat", "main")
    state = State.load(ch.state_dir)
    state.held_lane = "main"
    state.held_acquire_step = "lane"
    state.step = "flip"
    state.status = "running"                     # died mid-phase, still holding
    state.lane_restore = LaneRestore(            # a still-matching crash leftover
        step="flip",
        base_head=worktree.head_of(tmp_path, "main"),
        quest_head=_worktree_head(ch))
    state.save(ch.state_dir)

    assert anyio.run(run_quest, ch) is RunResult.COMPLETED

    assert _steps_started(ch) == ["lane", "alpha", "beta", "flip", "gamma"]
    events = list(JsonlEventLog(ch.events_path).read())
    assert not any(e["type"] == "lane_restored" for e in events)
    assert State.load(ch.state_dir).lane_restore is None
