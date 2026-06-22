from pathlib import Path

import anyio

import imp.remote.session.attach as attach_mod
from imp.agents.tools.wait_for_user import AwaitingUserInput
from imp.engine import merge_queue as mq
from imp.engine.change import Change
from imp.engine.executor import _release_lane, _route_acquire
from imp.engine.state import InMemoryEventLog, JsonlEventLog, State
from imp.engine.types import Continue, Pause, Rule, SourceLoc, Step
from tests.conftest import _seed_control


def _acquire_step():
    loc = SourceLoc(file=Path("t"), line=1)
    return Step(name="lane", phase="merge", kind="branch",
                rules=(Rule(condition=None, action=Continue(), mutations=(), source=loc),),
                on_max_retries=Pause(), max_loops=1000, source=loc, acquire="base")


def _release_step():
    loc = SourceLoc(file=Path("t"), line=1)
    return Step(name="resolver", phase="merge", kind="agent",
                rules=(), on_max_retries=Pause(), max_loops=1000, source=loc,
                release="base")


async def _never_returns(_pred):
    raise AwaitingUserInput()


def test_acquire_head_marks_hold_and_continues(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="x", change_id="feat", base_branch="main")
    state, log = State.load(ch.state_dir), JsonlEventLog(ch.events_path)
    action, _fb, _muts = anyio.run(
        _route_acquire, _acquire_step(), ch, state, log, _never_returns)
    assert isinstance(action, Continue)
    assert mq.is_head(tmp_path, "feat", "main") is True
    # mark set in memory AND persisted
    assert state.held_lane == "main"
    assert state.held_acquire_step == "lane"
    assert State.load(ch.state_dir).held_lane == "main"


def test_acquire_non_head_hibernate_does_not_mark_hold(tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    # Place another change at head so "feat" is NOT head.
    Change.create(tmp_path, title="first", change_id="first", base_branch="main")
    mq.enqueue_if_absent(tmp_path, "first", "main")   # head
    ch = Change.create(tmp_path, title="x", change_id="feat", base_branch="main")
    mq.enqueue_if_absent(tmp_path, "feat", "main")    # NOT head
    state, log = State.load(ch.state_dir), JsonlEventLog(ch.events_path)
    action, _fb, _muts = anyio.run(
        _route_acquire, _acquire_step(), ch, state, log, _never_returns)
    assert isinstance(action, Pause)
    assert action.reason == "awaiting_turn"
    assert state.held_lane is None      # hold mark must NOT be set on hibernate
    assert state.held_acquire_step is None


def test_acquire_becomes_head_after_wait_marks_hold(tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    Change.create(tmp_path, title="first", change_id="first", base_branch="main")
    mq.enqueue_if_absent(tmp_path, "first", "main")   # head
    ch = Change.create(tmp_path, title="x", change_id="feat", base_branch="main")
    mq.enqueue_if_absent(tmp_path, "feat", "main")    # NOT head initially

    # Simulate: wait_for_turn_fn is called, the turn is given (first pops itself),
    # then _is_head() returns True.
    async def _pop_first_and_return(pred):
        mq.pop_and_wake(tmp_path, "first", "main")  # simulate first completing

    monkeypatch.setattr(
        "imp.remote.session.attach.spawn_session_daemon", lambda *a, **k: None)
    state, log = State.load(ch.state_dir), JsonlEventLog(ch.events_path)
    action, _fb, _muts = anyio.run(
        _route_acquire, _acquire_step(), ch, state, log, _pop_first_and_return)
    assert isinstance(action, Continue)
    assert state.held_lane == "main"
    assert state.held_acquire_step == "lane"


def test_release_pops_and_clears_mark(tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    a = Change.create(tmp_path, title="a", change_id="aaa", base_branch="main")
    Change.create(tmp_path, title="b", change_id="bbb", base_branch="main")
    mq.enqueue_if_absent(tmp_path, "aaa", "main")   # head
    mq.enqueue_if_absent(tmp_path, "bbb", "main")   # next
    # popping the head wakes the next head; stub the real daemon spawn so the
    # test never launches a detached `python -m imp.remote.session.daemon`.
    monkeypatch.setattr(attach_mod, "spawn_session_daemon", lambda *a, **k: None)
    state, log = State.load(a.state_dir), JsonlEventLog(a.events_path)
    state.held_lane = "main"
    state.held_acquire_step = "lane"
    _release_lane(_release_step(), a, state, log)
    assert all(e["change_id"] != "aaa" for e in mq.read(tmp_path))   # self popped
    assert state.held_lane is None
    assert state.held_acquire_step is None
    assert State.load(a.state_dir).held_lane is None
    assert State.load(a.state_dir).held_acquire_step is None


def test_rule_release_pops_and_clears_mark(tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    a = Change.create(tmp_path, title="a", change_id="aaa", base_branch="main")
    Change.create(tmp_path, title="b", change_id="bbb", base_branch="main")
    mq.enqueue_if_absent(tmp_path, "aaa", "main")   # head
    mq.enqueue_if_absent(tmp_path, "bbb", "main")   # next
    monkeypatch.setattr(attach_mod, "spawn_session_daemon", lambda *a, **k: None)
    from imp.engine.executor import _release_held_lane
    state, log = State.load(a.state_dir), InMemoryEventLog()
    state.held_lane = "main"
    state.held_acquire_step = "wait-for-head"
    _release_held_lane("base", a, state, log, step_name="integrator")
    assert all(e["change_id"] != "aaa" for e in mq.read(tmp_path))   # self popped
    assert state.held_lane is None
    assert State.load(a.state_dir).held_lane is None
    released = [e for e in log.read() if e["type"] == "lane_released"]
    assert released and released[-1]["payload"]["step"] == "integrator"
