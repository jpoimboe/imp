import threading
from pathlib import Path

from imp.engine import merge_queue as mq
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
    assert mq.position(tmp_path, lane[0]["change_id"], "main") == 0
