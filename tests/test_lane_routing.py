from pathlib import Path

import anyio

import imp.remote.session.attach as attach_mod
from imp.engine import merge_queue as mq
from imp.engine import worktree
from imp.engine.awaiting_user_input import AwaitingUserInput
from imp.engine.executor import _release_lane, _route_acquire, _worktree_head
from imp.engine.quest import Quest
from imp.engine.state import InMemoryEventLog, JsonlEventLog, LaneRestore, State
from imp.engine.types import (
    Continue,
    Goto,
    Pause,
    Phase,
    Rule,
    SourceLoc,
    Step,
    Vars,
    Workflow,
    WorkflowDefaults,
)
from tests.conftest import _branch_with_commit, _git, _seed_control


def _acquire_step():
    loc = SourceLoc(file=Path("t"), line=1)
    return Step(name="lane", phase="merge", kind="branch",
                rules=(Rule(condition=None, action=Continue(), mutations=(), source=loc),),
                on_max_retries=Pause(), max_loops=1000, source=loc, acquire="merge-lane")


def _release_step():
    loc = SourceLoc(file=Path("t"), line=1)
    return Step(name="resolver", phase="merge", kind="agent",
                rules=(), on_max_retries=Pause(), max_loops=1000, source=loc,
                release="merge-lane")


def _wf(*step_names: str) -> Workflow:
    loc = SourceLoc(file=Path("t"), line=1)
    steps = tuple(
        Step(name=n, phase="merge", kind="branch", rules=(),
             on_max_retries=Pause(), max_loops=1000, source=loc)
        for n in step_names)
    return Workflow(
        name="w", description="",
        phases=(Phase(name="merge", steps=steps, source=loc),),
        vars=Vars(decls=()),
        defaults=WorkflowDefaults(max_retries=0, on_max_retries=Pause()),
        agents={}, workflow_dir=Path("."), source=loc)


async def _never_returns(_pred):
    raise AwaitingUserInput()


def test_acquire_head_marks_hold_and_continues(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    state, log = State.load(ch.state_dir), JsonlEventLog(ch.events_path)
    action, _fb, _muts = anyio.run(
        _route_acquire, _acquire_step(), _wf("lane"), ch, state, log, _never_returns)
    assert isinstance(action, Continue)
    assert mq.is_head(tmp_path, "feat", "main") is True
    # mark set in memory AND persisted
    assert state.held_lane == "main"
    assert state.held_acquire_step == "lane"
    assert State.load(ch.state_dir).held_lane == "main"


def test_acquire_non_head_hibernate_does_not_mark_hold(tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    # Place another change at head so "feat" is NOT head.
    Quest.create(tmp_path, title="first", quest_id="first", base_branch="main")
    mq.enqueue_if_absent(tmp_path, "first", "main")   # head
    ch = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    mq.enqueue_if_absent(tmp_path, "feat", "main")    # NOT head
    state, log = State.load(ch.state_dir), JsonlEventLog(ch.events_path)
    action, _fb, _muts = anyio.run(
        _route_acquire, _acquire_step(), _wf("lane"), ch, state, log, _never_returns)
    assert isinstance(action, Pause)
    assert action.reason == "awaiting_turn"
    assert state.held_lane is None      # hold mark must NOT be set on hibernate
    assert state.held_acquire_step is None


def test_acquire_becomes_head_after_wait_marks_hold(tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    Quest.create(tmp_path, title="first", quest_id="first", base_branch="main")
    mq.enqueue_if_absent(tmp_path, "first", "main")   # head
    ch = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    mq.enqueue_if_absent(tmp_path, "feat", "main")    # NOT head initially

    # Simulate: wait_for_turn_fn is called, the turn is given (first pops itself),
    # then _is_head() returns True.
    async def _pop_first_and_return(pred):
        mq.pop_and_wake(tmp_path, "first", "main")  # simulate first completing

    monkeypatch.setattr(
        "imp.remote.session.attach.spawn_session_daemon", lambda *a, **k: None)
    state, log = State.load(ch.state_dir), JsonlEventLog(ch.events_path)
    action, _fb, _muts = anyio.run(
        _route_acquire, _acquire_step(), _wf("lane"), ch, state, log, _pop_first_and_return)
    assert isinstance(action, Continue)
    assert state.held_lane == "main"
    assert state.held_acquire_step == "lane"


def test_release_pops_and_clears_mark(tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    a = Quest.create(tmp_path, title="a", quest_id="aaa", base_branch="main")
    Quest.create(tmp_path, title="b", quest_id="bbb", base_branch="main")
    mq.enqueue_if_absent(tmp_path, "aaa", "main")   # head
    mq.enqueue_if_absent(tmp_path, "bbb", "main")   # next
    # popping the head wakes the next head; stub the real daemon spawn so the
    # test never launches a detached `python -m imp.remote.session.daemon`.
    monkeypatch.setattr(attach_mod, "spawn_session_daemon", lambda *a, **k: None)
    state, log = State.load(a.state_dir), JsonlEventLog(a.events_path)
    state.held_lane = "main"
    state.held_acquire_step = "lane"
    _release_lane(_release_step(), a, state, log)
    assert all(e["quest_id"] != "aaa" for e in mq.read(tmp_path))   # self popped
    assert state.held_lane is None
    assert state.held_acquire_step is None
    assert State.load(a.state_dir).held_lane is None
    assert State.load(a.state_dir).held_acquire_step is None


def test_rule_release_pops_and_clears_mark(tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    a = Quest.create(tmp_path, title="a", quest_id="aaa", base_branch="main")
    Quest.create(tmp_path, title="b", quest_id="bbb", base_branch="main")
    mq.enqueue_if_absent(tmp_path, "aaa", "main")   # head
    mq.enqueue_if_absent(tmp_path, "bbb", "main")   # next
    monkeypatch.setattr(attach_mod, "spawn_session_daemon", lambda *a, **k: None)
    from imp.engine.executor import _release_held_lane
    state, log = State.load(a.state_dir), InMemoryEventLog()
    state.held_lane = "main"
    state.held_acquire_step = "wait-for-head"
    _release_held_lane("merge-lane", a, state, log, step_name="reconciler")
    assert all(e["quest_id"] != "aaa" for e in mq.read(tmp_path))   # self popped
    assert state.held_lane is None
    assert State.load(a.state_dir).held_lane is None
    released = [e for e in log.read() if e["type"] == "lane_released"]
    assert released and released[-1]["payload"]["step"] == "reconciler"


def test_acquire_poll_reemits_lane_waiting_on_position_change(tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    Quest.create(tmp_path, title="first", quest_id="first", base_branch="main")
    Quest.create(tmp_path, title="second", quest_id="second", base_branch="main")
    mq.enqueue_if_absent(tmp_path, "first", "main")    # head
    mq.enqueue_if_absent(tmp_path, "second", "main")   # 2nd ahead
    ch = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    mq.enqueue_if_absent(tmp_path, "feat", "main")     # 3rd -> ahead == 2
    # popping a head wakes the next head; stub the real daemon spawn.
    monkeypatch.setattr(attach_mod, "spawn_session_daemon", lambda *a, **k: None)

    # Fake wait_for_turn_fn: poll the predicate, draining the lane between polls.
    async def _drain(pred):
        assert pred() is False                       # ahead still 2 -> no new emit
        mq.pop_and_wake(tmp_path, "first", "main")
        assert pred() is False                        # ahead now 1 -> emits ahead=1
        assert pred() is False                        # ahead still 1 -> no new emit
        mq.pop_and_wake(tmp_path, "second", "main")
        assert pred() is True                         # ahead now 0 (head) -> emits ahead=0

    state, log = State.load(ch.state_dir), InMemoryEventLog()
    action, _fb, _muts = anyio.run(
        _route_acquire, _acquire_step(), _wf("lane"), ch, state, log, _drain)

    assert isinstance(action, Continue)
    aheads = [e["payload"]["ahead"] for e in log.read() if e["type"] == "lane_waiting"]
    # Pre-wait emit (2) seeds the dedup; each *changed* poll emits exactly once;
    # unchanged polls emit nothing. One entry per position change, not per poll.
    assert aheads == [2, 1, 0]


def test_acquire_emits_glyph_in_lane_acquired_payload(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    loc = SourceLoc(file=Path("t"), line=1)
    step = Step(name="lane", phase="merge", kind="branch",
                rules=(Rule(condition=None, action=Continue(), mutations=(), source=loc),),
                on_max_retries=Pause(), max_loops=1000, source=loc,
                acquire="merge-lane", glyph="⛙")
    ch = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    state, log = State.load(ch.state_dir), InMemoryEventLog()
    anyio.run(_route_acquire, step, _wf("lane"), ch, state, log, _never_returns)
    acquired = [e for e in log.read() if e["type"] == "lane_acquired"]
    assert len(acquired) == 1
    assert acquired[0]["payload"]["glyph"] == "⛙"


def test_acquire_lane_acquired_payload_glyph_none_when_unset(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    state, log = State.load(ch.state_dir), InMemoryEventLog()
    anyio.run(_route_acquire, _acquire_step(), _wf("lane"), ch, state, log, _never_returns)
    acquired = [e for e in log.read() if e["type"] == "lane_acquired"]
    assert len(acquired) == 1
    assert acquired[0]["payload"]["glyph"] is None


def _restore_for(quest: Quest, step: str) -> LaneRestore:
    return LaneRestore(
        step=step,
        base_head=worktree.head_of(quest.control_root, quest.base_branch),
        quest_head=_worktree_head(quest))


def test_acquire_restores_the_cursor_when_the_fingerprint_matches(
        tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    state, log = State.load(ch.state_dir), InMemoryEventLog()
    state.lane_restore = _restore_for(ch, "summarizer")

    action, _fb, _muts = anyio.run(
        _route_acquire, _acquire_step(), _wf("lane", "summarizer"),
        ch, state, log, _never_returns)

    # The step's own rule says Continue; the restore overrides it with a forward goto.
    assert isinstance(action, Goto)
    assert action.target == "summarizer"
    assert state.lane_restore is None
    assert State.load(ch.state_dir).lane_restore is None
    restored = [e for e in log.read() if e["type"] == "lane_restored"]
    assert len(restored) == 1
    assert restored[0]["payload"] == {"base": "main", "step": "summarizer"}
    # The acquiring step closes its window like any other route.
    completed = [e for e in log.read() if e["type"] == "step_completed"]
    assert len(completed) == 1
    assert completed[0]["payload"]["step"] == "lane"
    assert completed[0]["payload"]["action"]["target"] == "summarizer"


def test_acquire_does_not_restore_when_the_base_moved(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    state, log = State.load(ch.state_dir), InMemoryEventLog()
    state.lane_restore = _restore_for(ch, "summarizer")
    (tmp_path / "moved.txt").write_text("x\n")           # advance the base branch
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base moved during the pause")

    action, _fb, _muts = anyio.run(
        _route_acquire, _acquire_step(), _wf("lane", "summarizer"),
        ch, state, log, _never_returns)

    assert isinstance(action, Continue)                  # today's path: on to rebase
    assert state.lane_restore is None
    assert State.load(ch.state_dir).lane_restore is None
    assert not any(e["type"] == "lane_restored" for e in log.read())


def test_acquire_does_not_restore_when_the_quest_head_moved(
        tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    state, log = State.load(ch.state_dir), InMemoryEventLog()
    state.lane_restore = _restore_for(ch, "summarizer")
    (ch.project_root / "edited.txt").write_text("x\n")   # move ONLY the worktree HEAD
    _git(ch.project_root, "add", "-A")
    _git(ch.project_root, "commit", "-qm", "human edited during the pause")
    assert worktree.head_of(tmp_path, "main") == state.lane_restore.base_head

    action, _fb, _muts = anyio.run(
        _route_acquire, _acquire_step(), _wf("lane", "summarizer"),
        ch, state, log, _never_returns)

    assert isinstance(action, Continue)
    assert state.lane_restore is None
    assert State.load(ch.state_dir).lane_restore is None
    assert not any(e["type"] == "lane_restored" for e in log.read())


def test_acquire_does_not_restore_when_the_base_ref_is_unresolvable(
        tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _branch_with_commit(tmp_path, "base")               # control root stays on main
    ch = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="base")
    state, log = State.load(ch.state_dir), InMemoryEventLog()
    state.lane_restore = _restore_for(ch, "summarizer")
    _git(tmp_path, "branch", "-D", "base")              # ref no longer resolves

    # No RuntimeError escapes _route_acquire.
    action, _fb, _muts = anyio.run(
        _route_acquire, _acquire_step(), _wf("lane", "summarizer"),
        ch, state, log, _never_returns)

    assert isinstance(action, Continue)
    assert state.lane_restore is None
    assert State.load(ch.state_dir).lane_restore is None
    assert not any(e["type"] == "lane_restored" for e in log.read())


def test_acquire_does_not_restore_when_the_target_step_is_gone(
        tmp_path: Path) -> None:
    """An imp upgrade during the pause can drop the recorded step from the packaged
    workflow while both SHAs still match — neither ref covers imp's own assets.
    Honoring it would hand run_quest a Goto that resolve_goto cannot resolve."""
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    state, log = State.load(ch.state_dir), InMemoryEventLog()
    state.lane_restore = _restore_for(ch, "summarizer")   # fingerprint DOES match

    action, _fb, _muts = anyio.run(
        _route_acquire, _acquire_step(), _wf("lane"),     # "summarizer" is gone
        ch, state, log, _never_returns)

    assert isinstance(action, Continue)                   # falls through to the rule
    assert state.lane_restore is None
    assert State.load(ch.state_dir).lane_restore is None
    assert not any(e["type"] == "lane_restored" for e in log.read())
