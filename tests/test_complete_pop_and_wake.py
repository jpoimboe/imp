
import imp.remote.session.attach as attach_mod
from imp.engine import merge_queue as mq
from imp.engine.change import Change
from imp.engine.executor import _complete
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
