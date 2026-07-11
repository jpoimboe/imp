from pathlib import Path

import anyio

import imp.remote.session.attach as attach_mod
from imp.engine import merge_queue as mq
from imp.engine.awaiting_user_input import AwaitingUserInput
from imp.engine.executor import _release_lane, _route_acquire
from imp.engine.quest import Quest
from imp.engine.state import InMemoryEventLog, JsonlEventLog, State
from imp.engine.types import Continue, Pause, Rule, SourceLoc, Step
from tests.conftest import _seed_control


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


async def _never_returns(_pred):
    raise AwaitingUserInput()


def test_acquire_head_marks_hold_and_continues(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
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
    Quest.create(tmp_path, title="first", quest_id="first", base_branch="main")
    mq.enqueue_if_absent(tmp_path, "first", "main")   # head
    ch = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
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
        _route_acquire, _acquire_step(), ch, state, log, _pop_first_and_return)
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
        _route_acquire, _acquire_step(), ch, state, log, _drain)

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
    anyio.run(_route_acquire, step, ch, state, log, _never_returns)
    acquired = [e for e in log.read() if e["type"] == "lane_acquired"]
    assert len(acquired) == 1
    assert acquired[0]["payload"]["glyph"] == "⛙"


def test_acquire_lane_acquired_payload_glyph_none_when_unset(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    state, log = State.load(ch.state_dir), InMemoryEventLog()
    anyio.run(_route_acquire, _acquire_step(), ch, state, log, _never_returns)
    acquired = [e for e in log.read() if e["type"] == "lane_acquired"]
    assert len(acquired) == 1
    assert acquired[0]["payload"]["glyph"] is None
