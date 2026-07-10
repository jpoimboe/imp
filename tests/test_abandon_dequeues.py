import imp.remote.session.attach as attach_mod
from imp.engine import merge_queue as mq
from imp.engine.abandon import tear_down_change
from imp.engine.quest import Quest
from tests.conftest import _seed_control


def test_abandon_head_removes_and_wakes_next(tmp_path, monkeypatch) -> None:
    _seed_control(tmp_path)
    a = Quest.create(tmp_path, title="a", quest_id="aaa", base_branch="main")
    Quest.create(tmp_path, title="b", quest_id="bbb", base_branch="main")
    mq.enqueue_if_absent(tmp_path, "aaa", "main")
    mq.enqueue_if_absent(tmp_path, "bbb", "main")
    woken: list = []
    monkeypatch.setattr(attach_mod, "spawn_session_daemon",
                        lambda change, **kw: woken.append(change.id))

    tear_down_change(a)

    assert all(e["quest_id"] != "aaa" for e in mq.read(tmp_path))   # removed
    assert woken == ["bbb"]                                          # next head woken


def test_abandon_not_in_queue_is_noop(tmp_path, monkeypatch) -> None:
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="x", quest_id="xxx", base_branch="main")
    woken: list = []
    monkeypatch.setattr(attach_mod, "spawn_session_daemon", lambda *a, **k: woken.append(1))
    # Not enqueued — pop_and_wake should be a no-op, no wake
    tear_down_change(ch)
    assert mq.read(tmp_path) == []
    assert woken == []


def test_abandon_non_head_removes_without_waking(tmp_path, monkeypatch) -> None:
    _seed_control(tmp_path)
    Quest.create(tmp_path, title="a", quest_id="aaa", base_branch="main")
    b = Quest.create(tmp_path, title="b", quest_id="bbb", base_branch="main")
    mq.enqueue_if_absent(tmp_path, "aaa", "main")
    mq.enqueue_if_absent(tmp_path, "bbb", "main")
    woken: list = []
    monkeypatch.setattr(attach_mod, "spawn_session_daemon", lambda *a, **k: woken.append(1))

    tear_down_change(b)

    assert [e["quest_id"] for e in mq.read(tmp_path)] == ["aaa"]    # only b removed
    assert woken == []                                              # head untouched
