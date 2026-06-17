from pathlib import Path

import anyio
import pytest

from imp.agents.tools.wait_for_user import AwaitingUserInput
from imp.engine import merge_queue as mq
from imp.engine.change import Change
from imp.engine.executor import _headless_wait_for_turn, _route_wait_for_head
from imp.engine.state import JsonlEventLog, State
from imp.engine.types import Continue, Pause, Rule, SourceLoc, Step
from tests.conftest import _git, _seed_control


def _wait_step():
    loc = SourceLoc(file=Path("t"), line=1)
    return Step(name="wait-for-head", phase="merge", kind="wait_for_head",
                rules=(Rule(condition=None, action=Continue(), mutations=(), source=loc),),
                on_max_retries=Pause(), max_loops=1000, source=loc)


def _ctx(ch):
    return State.load(ch.state_dir), JsonlEventLog(ch.events_path)


async def _never_returns(_predicate):
    raise AwaitingUserInput()       # stand-in for the unattended hibernate path


def test_head_proceeds_with_continue(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="x", change_id="feat", base_branch="main")
    state, log = _ctx(ch)
    action, _fb, _muts = anyio.run(
        _route_wait_for_head, _wait_step(), ch, state, log, _never_returns)
    # only entry on the lane -> head -> Continue (never touches wait_for_turn_fn)
    assert isinstance(action, Continue)
    assert mq.is_head(tmp_path, "feat", "main") is True


def test_non_head_unattended_hibernates(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Change.create(tmp_path, title="a", change_id="aaa", base_branch="main")
    ch = Change.create(tmp_path, title="b", change_id="bbb", base_branch="main")
    mq.enqueue_if_absent(tmp_path, "aaa", "main")        # 'aaa' is head ahead of us
    state, log = _ctx(ch)
    action, _fb, _muts = anyio.run(
        _route_wait_for_head, _wait_step(), ch, state, log, _never_returns)
    assert isinstance(action, Pause)
    assert action.reason == "awaiting_turn"
    events = [e.get("type") for e in JsonlEventLog(ch.events_path).read()]
    assert "waiting_for_head" in events                  # rendered the wait, then hibernated


def test_headless_wait_for_turn_returns_or_raises() -> None:
    anyio.run(_headless_wait_for_turn, lambda: True)     # head: returns
    with pytest.raises(AwaitingUserInput):
        anyio.run(_headless_wait_for_turn, lambda: False)  # not head, no client: hibernate


def test_non_head_becomes_head_then_continues(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Change.create(tmp_path, title="a", change_id="aaa", base_branch="main")
    ch = Change.create(tmp_path, title="b", change_id="bbb", base_branch="main")
    mq.enqueue_if_absent(tmp_path, "aaa", "main")        # 'aaa' is head ahead of us
    state, log = _ctx(ch)

    async def _instant_turn_fn(_pred):
        # Simulates: predicate becomes True after waiting (daemon woke us).
        # Returns normally so the route falls through to evaluate_rules -> Continue.
        return

    action, _fb, _muts = anyio.run(
        _route_wait_for_head, _wait_step(), ch, state, log, _instant_turn_fn)
    assert isinstance(action, Continue)
    events = [e.get("type") for e in JsonlEventLog(ch.events_path).read()]
    assert "waiting_for_head" in events   # the wait was emitted
    assert "step_completed" in events     # and the step completed successfully
