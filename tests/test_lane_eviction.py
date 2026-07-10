from pathlib import Path

import anyio

import imp.remote.session.attach as attach_mod
from imp.engine import merge_queue as mq
from imp.engine.change import Change
from imp.engine.executor import RunResult, _evict_if_holding, run_change
from imp.engine.state import JsonlEventLog, State
from tests.conftest import _git, _seed_control


def test_evict_releases_lane_and_rewinds(tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    a = Change.create(tmp_path, title="a", quest_id="aaa", base_branch="main")
    Change.create(tmp_path, title="b", quest_id="bbb", base_branch="main")
    mq.enqueue_if_absent(tmp_path, "aaa", "main")   # head
    mq.enqueue_if_absent(tmp_path, "bbb", "main")   # next
    # eviction pops the head and wakes the next head; stub the real daemon spawn.
    monkeypatch.setattr(attach_mod, "spawn_session_daemon", lambda *a, **k: None)
    state = State.load(a.state_dir)
    state.held_lane = "main"
    state.held_acquire_step = "lane"
    state.step = "touch-base"            # somewhere inside the held region
    state.resume_pending = "touch-base"
    log = JsonlEventLog(a.events_path)

    evicted = _evict_if_holding(a, state, log)

    assert evicted is True
    assert all(e["quest_id"] != "aaa" for e in mq.read(tmp_path))   # lane released
    assert state.step == "lane"          # rewound to the acquiring step
    assert state.resume_pending is None  # not an in-place resume
    assert state.held_lane is None
    assert state.held_acquire_step is None


def test_evict_noop_when_not_holding(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    state = State.load(ch.state_dir)      # held_lane is None
    assert _evict_if_holding(ch, state, JsonlEventLog(ch.events_path)) is False


def _write_serialize_workflow(root: Path, touch_cmd: str) -> None:
    wf = root / "imp" / "workflows" / "imp" / "workflow.yaml"
    wf.write_text(
        "name: t\ndescription: \"\"\n"
        "phases:\n"
        "  - name: serialize\n"
        "    steps:\n"
        "      - name: lane\n"
        "        acquire: merge-lane\n"
        "        rules:\n"
        "          - {else: true, continue: true}\n"
        "      - name: touch-base\n"
        f"        command: \"{touch_cmd}\"\n"
        "        rules:\n"
        "          - {if: {exit: 0}, continue: true}\n"
        "          - {else: true, pause: true}\n"
    )
    # keep the script gate deterministic across environments
    (root / ".imp" / "config.yaml").write_text(
        (root / ".imp" / "config.yaml").read_text()
        + "sandbox:\n  enabled: false\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "serialize workflow")


def test_pause_in_held_region_evicts_and_rewinds(tmp_path: Path, provider_registry) -> None:
    from tests.test_executor_rename import _FakeProvider
    provider_registry("anthropic", lambda: True, lambda model: _FakeProvider(model))
    _seed_control(tmp_path)
    _write_serialize_workflow(tmp_path, "false")   # touch-base fails -> pause
    ch = Change.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    result = anyio.run(run_change, ch)
    assert result is RunResult.PAUSED
    reloaded = State.load(ch.state_dir)
    assert reloaded.step == "lane"                 # resume cursor rewound to acquire
    assert reloaded.held_lane is None              # lane released in-process
    assert mq.read(tmp_path) == []                 # the sole holder was popped


def test_crash_resume_with_held_mark_evicts_at_entry(tmp_path: Path, provider_registry) -> None:
    from tests.test_executor_rename import _FakeProvider
    provider_registry("anthropic", lambda: True, lambda model: _FakeProvider(model))
    _seed_control(tmp_path)
    _write_serialize_workflow(tmp_path, "true")    # touch-base succeeds
    ch = Change.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    # Simulate a crash WHILE holding: mark set, cursor mid-held-region, still running.
    mq.enqueue_if_absent(tmp_path, "feat", "main")
    state = State.load(ch.state_dir)
    state.held_lane = "main"
    state.held_acquire_step = "lane"
    state.step = "touch-base"
    state.status = "running"
    state.save(ch.state_dir)

    anyio.run(run_change, ch)
    types_seen = [e.get("type") for e in JsonlEventLog(ch.events_path).read()]
    assert "lane_evicted" in types_seen            # entry eviction fired on resume
    # After eviction the change re-acquires, runs touch-base, and completes with
    # no merge — the no-merge completion path must release the lane (AC2), so the
    # queue is empty rather than leaking the re-acquired entry.
    assert State.load(ch.state_dir).status == "completed"
    assert mq.read(tmp_path) == []
