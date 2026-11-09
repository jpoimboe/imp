"""The one-shot 16 -> 17 upgrade: quests parked on the retired `converge-verify`
step move to `wait-for-head`, in live homes and resumable `archived` records."""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from imp.cli.store import LocalStore
from imp.engine import archive, merge_queue
from imp.engine.abandon import archive_conversation
from imp.engine.archive import index, record
from imp.engine.archive import read as archive_read
from imp.engine.errors import ArchiveMismatch
from imp.engine.executor import RunResult, run_quest
from imp.engine.quest import LAYOUT_VERSION, Quest
from imp.engine.state import State
from tests.conftest import BELOW_DATA_FLOOR, _seed_control, write_agent_md
from tests.test_default_workflow_e2e_mocked import _run_events, _seed_default_workflow

QA = "arch01"   # archived at converge-verify: healed
QO = "arch02"   # archived at another step
QC = "cmpl01"   # completed at converge-verify: history
QB = "abnd01"   # abandoned at converge-verify: history

PARK: dict[str, Any] = {
    "resume_target": "agent", "resume_should_continue": True,
    "retry_feedback": {"target_step": "converge-verify", "text": "slow gate failed"},
    "route_label": "back to tests", "unsandboxed": True,
    "step_label": "waiting to test", "phase_label": "converge phase",
}
HEALED: dict[str, Any] = {
    "step": "wait-for-head", "resume_pending": None, "resume_target": None,
    "resume_should_continue": False, "retry_feedback": None, "route_label": None,
    "unsandboxed": False, "step_label": "waiting in merge queue",
    "phase_label": "integration phase",
}
FORK = (
    'name: fork\ndescription: ""\nphases:\n  - name: p\n    steps:\n'
    "      - {name: converge-verify, file: agents/stub.md}\n"
    "      - {name: wait-for-head, file: agents/stub.md}\n")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _dump(data: dict[str, Any]) -> bytes:
    return json.dumps(data, indent=2, sort_keys=True).encode("utf-8")


def _marker(control: Path) -> str:
    return archive.data_version_path(control).read_text().strip()


def _user_version(control: Path) -> int:
    conn = sqlite3.connect(archive.index_path(control))
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def _index_step(control: Path, quest_id: str) -> str:
    conn = sqlite3.connect(archive.index_path(control))
    try:
        return str(conn.execute(
            "SELECT step FROM quests WHERE id = ?", (quest_id,)).fetchone()[0])
    finally:
        conn.close()


def _record_bytes(control: Path, quest_id: str) -> bytes:
    return (archive.records_root(control) / f"{quest_id}.json").read_bytes()


def _fork_default(control: Path) -> None:
    wf = control / "imp" / "workflows" / "default"
    write_agent_md(wf / "agents" / "stub.md", frontmatter=(
        "name: stub\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}"))
    (wf / "workflow.yaml").write_text(FORK)


def _draft_at(control: Path, qid: str, step: str, *,
              workflow: str = "default", **extra: Any) -> Quest:
    q = Quest.create_draft(control, title=qid, quest_id=qid, base_branch="main")
    meta_path = q.quest_dir / "quest.json"
    meta = json.loads(meta_path.read_text())
    meta["workflow"] = workflow
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True))
    state_path = q.state_dir / "state.json"
    data = json.loads(state_path.read_text())
    data.update(step=step, resume_pending=step, **extra)
    state_path.write_bytes(_dump(data))
    return q


def _parked_home(control: Path, qid: str, step: str = "converge-verify", *,
                 workflow: str = "default") -> Path:
    q = _draft_at(control, qid, step, workflow=workflow,
                  status="paused", halt_condition="waiting", **PARK)
    return q.state_dir / "state.json"


def _archive_finished(control: Path, qid: str, status: str) -> None:
    src = control / "seed" / qid
    src.mkdir(parents=True)
    (src / "events.jsonl").write_text("")
    (src / "quest.json").write_text(json.dumps({
        "id": qid, "title": qid, "base_branch": "main", "workflow": "default",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []}))
    (src / "state.json").write_text(json.dumps(
        {"id": qid, "status": status, "step": "converge-verify", **PARK}))
    archive.archive_quest(
        control, qid, events_path=src / "events.jsonl",
        quest_json_path=src / "quest.json", state_json_path=src / "state.json")


def _seed_v16_archive(control: Path) -> None:
    """Real archived quests, rolled back to exactly how a v16 install left them."""
    _seed_control(control)
    archive_conversation(_draft_at(control, QA, "converge-verify", **PARK))
    archive_conversation(_draft_at(control, QO, "spec"))
    _archive_finished(control, QC, "completed")
    _archive_finished(control, QB, "abandoned")
    conn = sqlite3.connect(archive.index_path(control))
    try:
        conn.execute("PRAGMA user_version = 9")     # the index floor v16 shipped
        conn.commit()
    finally:
        conn.close()
    archive._write_data_version(control, 16)


def test_a_blob_backed_read_heals_archived_records_and_reindexes(tmp_path: Path) -> None:
    _seed_v16_archive(tmp_path)
    before = record.read_record(archive.records_root(tmp_path), QA)
    assert before is not None
    keep = {q: _record_bytes(tmp_path, q) for q in (QO, QC, QB)}
    assert _index_step(tmp_path, QA) == "converge-verify"    # falsifiable: stale before
    assert _user_version(tmp_path) == 9

    archive_read.status_of(tmp_path, QA)

    healed = record.read_record(archive.records_root(tmp_path), QA)
    assert healed == {**before, "final_state": {**before["final_state"], **HEALED}}
    assert healed["final_state"]["status"] == "archived"
    for qid, raw in keep.items():
        assert _record_bytes(tmp_path, qid) == raw, qid
    assert _index_step(tmp_path, QA) == "wait-for-head"
    assert (archive._DATA_VERSION, archive._UPGRADABLE_FROM) == (17, 16)
    assert _marker(tmp_path) == "17"
    assert _user_version(tmp_path) == index._SCHEMA_VERSION == 10


def test_ensure_current_alone_runs_the_hop(tmp_path: Path) -> None:
    _seed_v16_archive(tmp_path)

    archive_read.completed_rows(tmp_path)     # index-only: no check_data_floor

    assert _marker(tmp_path) == "17"
    assert _index_step(tmp_path, QA) == "wait-for-head"


def test_the_same_pass_heals_unlocked_live_homes(tmp_path: Path) -> None:
    _seed_v16_archive(tmp_path)
    parked = _parked_home(tmp_path, "park01")
    sweep = _parked_home(tmp_path, "swep01", "deferred-sweep")   # drains, then moves on
    before = json.loads(parked.read_text())
    sweep_bytes = sweep.read_bytes()

    archive_read.status_of(tmp_path, QA)

    assert parked.read_bytes() == _dump({**before, **HEALED})
    healed = json.loads(parked.read_text())
    assert (healed["status"], healed["halt_condition"]) == ("paused", "waiting")
    assert sweep.read_bytes() == sweep_bytes


def test_the_hop_skips_a_live_locked_home(tmp_path: Path) -> None:
    _seed_v16_archive(tmp_path)
    locked = _parked_home(tmp_path, "lock01")
    (locked.parents[1] / "lock").write_text(str(os.getpid()))   # a living holder
    raw = locked.read_bytes()

    archive_read.status_of(tmp_path, QA)

    assert _marker(tmp_path) == "17"
    assert locked.read_bytes() == raw


def test_the_hop_leaves_quests_on_a_fork_that_still_ships_the_step(tmp_path: Path) -> None:
    _seed_v16_archive(tmp_path)
    _fork_default(tmp_path)
    home = _parked_home(tmp_path, "fork01")
    raw_home, raw_record = home.read_bytes(), _record_bytes(tmp_path, QA)

    archive_read.status_of(tmp_path, QA)

    assert _marker(tmp_path) == "17"
    assert home.read_bytes() == raw_home
    assert _record_bytes(tmp_path, QA) == raw_record


def test_the_hop_leaves_a_workflow_that_does_not_resolve(tmp_path: Path) -> None:
    _seed_v16_archive(tmp_path)
    lost = _parked_home(tmp_path, "lost01", workflow="nosuch")
    raw = lost.read_bytes()

    archive_read.status_of(tmp_path, QA)

    assert _marker(tmp_path) == "17"
    assert lost.read_bytes() == raw


def test_a_rehydrated_healed_record_resumes_at_wait_for_head(tmp_path: Path) -> None:
    _seed_v16_archive(tmp_path)
    archive_read.status_of(tmp_path, QA)

    archive_read.rehydrate_quest(tmp_path, QA)

    state = State.load(Quest.load(tmp_path, QA).state_dir)
    assert state.step == "wait-for-head"
    assert (state.resume_pending, state.resume_should_continue) == (None, False)
    assert (state.step_label, state.phase_label) == (
        "waiting in merge queue", "integration phase")


@pytest.mark.anyio
async def test_the_migrated_live_quest_runs_into_wait_for_head(tmp_path: Path) -> None:
    quest = _seed_default_workflow(tmp_path)    # a workflow without the retired step
    path = quest.state_dir / "state.json"
    data = json.loads(path.read_text())
    data.update(step="converge-verify", status="paused",
                resume_pending="converge-verify", **PARK)
    path.write_bytes(_dump(data))
    archive._write_data_version(tmp_path, 16)
    archive.ensure_current(tmp_path)
    assert State.load(quest.state_dir).step == "wait-for-head"
    merge_queue.enqueue_if_absent(tmp_path, "ahead1", "main")   # park it at the lane

    result = await run_quest(quest, headless=True)

    events = _run_events(quest)
    started = [e["payload"]["step"] for e in events if e.get("type") == "step_started"]
    paused = [e["payload"] for e in events if e.get("type") == "workflow_paused"]
    assert result is RunResult.PAUSED
    assert started == ["wait-for-head"]
    assert paused == [{"reason": "awaiting_turn"}]         # not unknown_step
    assert State.load(quest.state_dir).step == "wait-for-head"


@pytest.mark.parametrize("with_marker", [True, False])
def test_scan_homes_heals_an_unlocked_parked_home(tmp_path: Path, with_marker: bool) -> None:
    _seed_control(tmp_path)
    if with_marker:
        archive._write_data_version(tmp_path, archive._DATA_VERSION)
    parked = _parked_home(tmp_path, "park01")
    locked = _parked_home(tmp_path, "lock01")
    (locked.parents[1] / "lock").write_text(str(os.getpid()))
    expected = _dump({**json.loads(parked.read_text()), **HEALED})
    raw_locked = locked.read_bytes()

    digests = {h.quest_id: h for h in LocalStore(tmp_path).scan_homes()}

    assert parked.read_bytes() == digests["park01"].state_json == expected
    assert locked.read_bytes() == raw_locked


def test_scan_homes_leaves_a_fork_or_an_unresolvable_workflow(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _fork_default(tmp_path)
    homes = [_parked_home(tmp_path, "fork01"),
             _parked_home(tmp_path, "lost01", workflow="nosuch")]
    raws = [h.read_bytes() for h in homes]

    LocalStore(tmp_path).scan_homes()

    assert [h.read_bytes() for h in homes] == raws


def test_an_unreadable_live_home_does_not_block_the_hop(tmp_path: Path) -> None:
    _seed_v16_archive(tmp_path)
    broken = _parked_home(tmp_path, "brok01")
    broken.write_bytes(b"{not json")
    parked = _parked_home(tmp_path, "park01")

    archive_read.status_of(tmp_path, QA)

    assert _marker(tmp_path) == "17"
    assert broken.read_bytes() == b"{not json"
    assert json.loads(parked.read_text())["step"] == "wait-for-head"


def test_a_crash_after_the_rebuild_leaves_the_marker_and_retries(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_v16_archive(tmp_path)
    real_rebuild = archive.rebuild_index

    def rebuilt_then_crash(root: Path) -> None:
        real_rebuild(root)
        raise RuntimeError("boom")

    monkeypatch.setattr(archive, "rebuild_index", rebuilt_then_crash)
    with pytest.raises(RuntimeError):
        archive_read.status_of(tmp_path, QA)
    assert _marker(tmp_path) == "16"
    assert archive._read_data_version(tmp_path) == 16   # never read as v0

    monkeypatch.setattr(archive, "rebuild_index", real_rebuild)  # undo() would drop IMP_HOME
    archive_read.status_of(tmp_path, QA)
    assert _marker(tmp_path) == "17"
    assert _index_step(tmp_path, QA) == "wait-for-head"


def test_a_second_pass_does_no_work(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_v16_archive(tmp_path)
    archive_read.status_of(tmp_path, QA)
    first = _record_bytes(tmp_path, QA)

    calls: list[int] = []
    monkeypatch.setattr(archive, "rebuild_index", lambda _root: calls.append(1))
    archive_read.status_of(tmp_path, QA)
    archive_read.completed_rows(tmp_path)

    assert calls == []                               # marker current -> fast path
    assert _record_bytes(tmp_path, QA) == first


def test_a_read_only_archive_is_left_untouched(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_v16_archive(tmp_path)
    raw = _record_bytes(tmp_path, QA)
    # The runner may be root, where mode bits deny nothing: deny the probe itself.
    real_access = os.access
    target = str(archive.archive_root(tmp_path))
    monkeypatch.setattr(
        archive.os, "access",
        lambda p, mode: False if (mode == os.W_OK and str(p) == target)
        else real_access(p, mode))

    with pytest.raises(ArchiveMismatch) as excinfo:
        archive_read.status_of(tmp_path, QA)
    archive_read.completed_rows(tmp_path)

    assert (excinfo.value.floor, excinfo.value.stored) == ("data", 16)
    assert _marker(tmp_path) == "16"
    assert _record_bytes(tmp_path, QA) == raw


def test_any_other_below_floor_version_still_raises(tmp_path: Path) -> None:
    _seed_v16_archive(tmp_path)
    raw = _record_bytes(tmp_path, QA)
    archive._write_data_version(tmp_path, BELOW_DATA_FLOOR)

    with pytest.raises(ArchiveMismatch) as excinfo:
        archive_read.status_of(tmp_path, QA)

    assert (excinfo.value.floor, excinfo.value.stored) == ("data", BELOW_DATA_FLOOR)
    assert _marker(tmp_path) == str(BELOW_DATA_FLOOR)
    assert _record_bytes(tmp_path, QA) == raw


def test_no_layout_bump_and_the_earlier_hops_are_retired() -> None:
    assert LAYOUT_VERSION == 23
    for name in ("heal_live_status", "_RETIRED_FINISHED", "_rename_live_status",
                 "_backfill_commits", "_commits_of", "_serialize_events"):
        assert not hasattr(archive, name), name
    tests = Path(__file__).parent
    assert not (tests / "test_archive_status_migration.py").exists()
    assert not (tests / "test_archive_commits_migration.py").exists()
