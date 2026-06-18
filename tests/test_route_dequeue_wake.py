from pathlib import Path

from imp.engine import merge_queue as mq
from imp.engine.change import Change
from imp.engine.executor import _route_dequeue_wake
from imp.engine.state import JsonlEventLog, State
from imp.engine.types import Continue, Pause, Rule, SourceLoc, Step
from tests.conftest import _seed_control


def _dequeue_step():
    loc = SourceLoc(file=Path("t"), line=1)
    return Step(name="bypass-dequeue", phase="merge", kind="dequeue_wake",
                rules=(Rule(condition=None, action=Continue(), mutations=(), source=loc),),
                on_max_retries=Pause(), max_loops=1000, source=loc)


def test_dequeue_wake_removes_self_and_continues(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="x", change_id="feat", base_branch="main")
    mq.enqueue_if_absent(tmp_path, "feat", "main")   # only entry -> no next to wake
    state = State.load(ch.state_dir)
    log = JsonlEventLog(ch.events_path)
    action, _fb, _muts = _route_dequeue_wake(_dequeue_step(), ch, state, log)
    assert isinstance(action, Continue)
    assert mq.read(tmp_path) == []                   # self removed from the queue
    events = [e.get("type") for e in JsonlEventLog(ch.events_path).read()]
    assert "step_started" in events
    assert "step_completed" in events
