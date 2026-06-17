from pathlib import Path

import imp.remote.session.attach as attach_mod
from imp.engine import merge_queue as mq
from imp.engine.abandon import tear_down_change
from imp.engine.change import Change
from tests.conftest import _seed_control


def test_abandon_head_removes_and_wakes_next(tmp_path, monkeypatch) -> None:
    _seed_control(tmp_path)
    a = Change.create(tmp_path, title="a", change_id="aaa", base_branch="main")
    Change.create(tmp_path, title="b", change_id="bbb", base_branch="main")
    mq.enqueue_if_absent(tmp_path, "aaa", "main")
    mq.enqueue_if_absent(tmp_path, "bbb", "main")
    woken: list = []
    monkeypatch.setattr(attach_mod, "spawn_session_daemon",
                        lambda change, **kw: woken.append(change.id))

    tear_down_change(a)

    assert all(e["change_id"] != "aaa" for e in mq.read(tmp_path))   # removed
    assert woken == ["bbb"]                                          # next head woken


def test_abandon_not_in_queue_is_noop(tmp_path, monkeypatch) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="x", change_id="xxx", base_branch="main")
    woken: list = []
    monkeypatch.setattr(attach_mod, "spawn_session_daemon", lambda *a, **k: woken.append(1))
    # Not enqueued — pop_and_wake should be a no-op, no wake
    tear_down_change(ch)
    assert mq.read(tmp_path) == []
    assert woken == []


def test_abandon_non_head_removes_without_waking(tmp_path, monkeypatch) -> None:
    _seed_control(tmp_path)
    Change.create(tmp_path, title="a", change_id="aaa", base_branch="main")
    b = Change.create(tmp_path, title="b", change_id="bbb", base_branch="main")
    mq.enqueue_if_absent(tmp_path, "aaa", "main")
    mq.enqueue_if_absent(tmp_path, "bbb", "main")
    woken: list = []
    monkeypatch.setattr(attach_mod, "spawn_session_daemon", lambda *a, **k: woken.append(1))

    tear_down_change(b)

    assert [e["change_id"] for e in mq.read(tmp_path)] == ["aaa"]    # only b removed
    assert woken == []                                              # head untouched
