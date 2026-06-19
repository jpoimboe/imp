import shutil
import threading
from pathlib import Path

from imp.engine import merge_queue as mq
from imp.engine.change import Change
from tests.conftest import _seed_control


def test_enqueue_then_head_is_oldest_for_base(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    mq.enqueue_if_absent(tmp_path, "a", "main")
    mq.enqueue_if_absent(tmp_path, "b", "main")
    assert mq.is_head(tmp_path, "a", "main") is True
    assert mq.is_head(tmp_path, "b", "main") is False
    assert mq.position(tmp_path, "a", "main") == 0
    assert mq.position(tmp_path, "b", "main") == 1


def test_enqueue_is_idempotent_by_change_id(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    mq.enqueue_if_absent(tmp_path, "a", "main")
    mq.enqueue_if_absent(tmp_path, "a", "main")
    assert len(mq.read(tmp_path)) == 1  # no duplicate created
    # one entry only — re-enqueue of a present id is a no-op (does not reorder)
    assert mq.position(tmp_path, "a", "main") == 0
    mq.enqueue_if_absent(tmp_path, "b", "main")
    assert mq.is_head(tmp_path, "a", "main") is True


def test_lanes_are_independent_by_base(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    mq.enqueue_if_absent(tmp_path, "a", "main")
    mq.enqueue_if_absent(tmp_path, "b", "release")
    # each lane's head is independent: both are heads of their own base
    assert mq.is_head(tmp_path, "a", "main") is True
    assert mq.is_head(tmp_path, "b", "release") is True


def test_concurrent_enqueue_preserves_all_entries(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ids = [f"c{i}" for i in range(20)]

    def _worker(cid: str) -> None:
        mq.enqueue_if_absent(tmp_path, cid, "main")

    threads = [threading.Thread(target=_worker, args=(cid,)) for cid in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    entries = mq.read(tmp_path)
    got = sorted(e["change_id"] for e in entries if e["base_branch"] == "main")
    assert got == sorted(ids)                          # no lost / no duplicated entries
    # ISO-8601 timestamps present and the lane is a strict FIFO by enqueued_at
    lane = [e for e in entries if e["base_branch"] == "main"]
    stamps = [e["enqueued_at"] for e in lane]
    assert all(isinstance(s, str) and "T" in s for s in stamps)
    assert stamps == sorted(stamps)
    assert mq.position(tmp_path, lane[0]["change_id"], "main") == 0


def test_pop_and_wake_removes_self_and_wakes_new_head(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    # Change.load runs before spawn_fn, so real worktrees are required.
    from imp.engine.change import Change
    Change.create(tmp_path, title="a", change_id="a", base_branch="main")
    Change.create(tmp_path, title="b", change_id="b", base_branch="main")
    mq.enqueue_if_absent(tmp_path, "a", "main")
    mq.enqueue_if_absent(tmp_path, "b", "main")
    woken: list[str] = []
    mq.pop_and_wake(
        tmp_path, "a", "main",
        spawn_fn=lambda change, **kw: woken.append((change.id, kw.get("resume"))),
    )
    assert all(e["change_id"] != "a" for e in mq.read(tmp_path))   # self removed
    assert woken == [("b", True)]                                  # new head woken with resume


def test_pop_and_wake_no_wake_when_lane_empties(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    mq.enqueue_if_absent(tmp_path, "a", "main")
    woken: list[str] = []
    mq.pop_and_wake(tmp_path, "a", "main", spawn_fn=lambda change, **kw: woken.append(change.id))
    assert mq.read(tmp_path) == []
    assert woken == []                                             # nothing left to wake


def test_pop_and_wake_no_wake_when_self_not_head(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    mq.enqueue_if_absent(tmp_path, "a", "main")
    mq.enqueue_if_absent(tmp_path, "b", "main")
    woken: list[str] = []
    # 'b' (not head) removes itself: the head 'a' is untouched and not re-woken.
    mq.pop_and_wake(tmp_path, "b", "main", spawn_fn=lambda change, **kw: woken.append(change.id))
    assert [e["change_id"] for e in mq.read(tmp_path)] == ["a"]
    assert woken == []


def test_remove_drops_entry(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    mq.enqueue_if_absent(tmp_path, "a", "main")
    mq.enqueue_if_absent(tmp_path, "b", "main")
    mq.remove(tmp_path, "a")
    assert [e["change_id"] for e in mq.read(tmp_path)] == ["b"]


def test_position_absent_returns_lane_length(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    mq.enqueue_if_absent(tmp_path, "a", "main")
    mq.enqueue_if_absent(tmp_path, "b", "main")
    assert mq.position(tmp_path, "absent", "main") == 2


def test_pop_and_wake_cross_lane_isolation(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    mq.enqueue_if_absent(tmp_path, "a", "main")
    mq.enqueue_if_absent(tmp_path, "b", "release")
    woken: list[str] = []
    # "main" lane empties after pop — no wake. "release" lane must be unaffected.
    mq.pop_and_wake(
        tmp_path, "a", "main",
        spawn_fn=lambda change, **kw: woken.append(change.id),
    )
    assert all(e["change_id"] != "a" for e in mq.read(tmp_path))   # "a" removed
    assert any(e["change_id"] == "b" for e in mq.read(tmp_path))   # "b" untouched
    assert woken == []                                               # no cross-lane wake


def test_remove_absent_is_noop(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    mq.enqueue_if_absent(tmp_path, "a", "main")
    mq.remove(tmp_path, "absent")
    assert [e["change_id"] for e in mq.read(tmp_path)] == ["a"]


def test_pop_and_wake_absent_noop(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    mq.enqueue_if_absent(tmp_path, "a", "main")
    mq.enqueue_if_absent(tmp_path, "b", "main")
    from unittest.mock import Mock
    spawn_mock = Mock()
    mq.pop_and_wake(tmp_path, "absent", "main", spawn_fn=spawn_mock)
    lane_ids = [e["change_id"] for e in mq.read(tmp_path) if e["base_branch"] == "main"]
    assert lane_ids == ["a", "b"]
    spawn_mock.assert_not_called()


def test_ensure_head_driven_spawns_current_head_with_resume(tmp_path) -> None:
    _seed_control(tmp_path)
    Change.create(tmp_path, title="a", change_id="a", base_branch="main")
    Change.create(tmp_path, title="b", change_id="b", base_branch="main")
    mq.enqueue_if_absent(tmp_path, "a", "main")
    mq.enqueue_if_absent(tmp_path, "b", "main")
    spawned: list = []
    mq.ensure_head_driven(
        tmp_path, "main",
        spawn_fn=lambda change, **kw: spawned.append((change.id, kw.get("resume"))),
    )
    assert spawned == [("a", True)]        # current head, woken with resume


def test_ensure_head_driven_empty_lane_is_noop(tmp_path) -> None:
    _seed_control(tmp_path)
    spawned: list = []
    mq.ensure_head_driven(
        tmp_path, "main", spawn_fn=lambda change, **kw: spawned.append(change.id))
    assert spawned == []                   # nothing to drive


def test_ensure_head_driven_tolerates_vanished_head(tmp_path) -> None:
    _seed_control(tmp_path)
    Change.create(tmp_path, title="a", change_id="a", base_branch="main")
    mq.enqueue_if_absent(tmp_path, "a", "main")
    shutil.rmtree(tmp_path / ".imp" / "worktrees" / "a")   # records gone → load fails
    spawned: list = []
    # Must not raise; the unloadable head is skipped silently.
    mq.ensure_head_driven(
        tmp_path, "main", spawn_fn=lambda change, **kw: spawned.append(change.id))
    assert spawned == []
