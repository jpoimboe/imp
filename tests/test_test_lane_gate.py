import fcntl
import json
import os
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import anyio
import pytest

import imp.cli.main as m
import imp.remote.session.attach as attach_mod
from imp.cli.signals import InterruptCoordinator
from imp.engine import merge_queue as mq
from imp.engine import test_lane
from imp.engine.executor import RunResult, run_quest
from imp.engine.quest import Quest
from imp.engine.state import JsonlEventLog, State
from imp.paths import project_state_dir
from tests.conftest import _git, _seed_control

_LANE_STEP = (
    "      - name: lane\n"
    "        acquire: merge-lane\n"
    "        rules:\n"
    "          - {else: true, continue: true}\n"
)


def _gate_step(command: str = "true", *, interruptible: bool = False,
               label: str | None = None) -> str:
    return (
        "      - name: run-tests\n"
        + (f"        label: {label}\n" if label is not None else "")
        + f"        command: \"{command}\"\n"
        + ("        interruptible: true\n" if interruptible else "")
        + "        acquire: test-lane\n"
        "        rules:\n"
        "          - {if: {exit: 0}, pause: true}\n"
        "          - {else: true, pause: true}\n"
    )


def _write_workflow(root: Path, steps: str, *, phase_label: str | None = None) -> None:
    wf = root / "imp" / "workflows" / "imp" / "workflow.yaml"
    wf.write_text(
        "name: t\ndescription: \"\"\n"
        "phases:\n"
        "  - name: gate\n"
        + (f"    label: {phase_label}\n" if phase_label is not None else "")
        + "    steps:\n" + steps
    )
    cfg_path = project_state_dir(root) / "config.yaml"
    cfg_path.write_text(cfg_path.read_text() + "sandbox:\n  enabled: false\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "test-lane workflow")


def _register_fake_provider(provider_registry) -> None:
    from tests.test_executor_rename import _FakeProvider
    provider_registry("anthropic", lambda: True, lambda model: _FakeProvider(model))


@pytest.fixture(autouse=True)
def polled(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    names: list[str] = []

    def _sleep() -> None:
        names.append(threading.current_thread().name)
        time.sleep(0.01)
    monkeypatch.setattr(test_lane, "_poll_sleep", _sleep)
    return names


@pytest.fixture
def no_daemon_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(attach_mod, "spawn_session_daemon", lambda *a, **k: None)


def _types(ch: Quest) -> list[str]:
    return [e.get("type") for e in JsonlEventLog(ch.events_path).read()]


def _priority_marker_held(root: Path) -> bool:
    # Serialize on `probe` like try_acquire, so a non-priority probe's transient lock isn't misread.
    d = root / ".git" / "imp" / "test-lane"
    probe = os.open(d / "probe", os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(probe, fcntl.LOCK_EX)
        fd = os.open(d / "priority", os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        finally:
            os.close(fd)
        return False
    finally:
        os.close(probe)


async def _until(pred: Callable[[], bool], what: str) -> None:
    for _ in range(400):
        if pred():
            return
        await anyio.sleep(0.025)
    raise AssertionError(f"timed out waiting for {what}")


def _snapshot_state(dest: Path) -> str:
    # The gate copies the persisted state.json, so a test reads the label the script ran under.
    return f"cp $IMP_QUEST_DIR/state/state.json {dest}"


def _labeled_quest(root: Path, command: str, *, interruptible: bool = False) -> Quest:
    _seed_control(root)
    _write_workflow(root, _gate_step(command, interruptible=interruptible,
                                     label="running tests"),
                    phase_label="integration phase")
    return Quest.create(root, title="x", quest_id="feat", base_branch="main")


def _persisted_label(ch: Quest) -> str | None:
    return State.load(ch.state_dir).step_label


async def _run_until_waiting_then_free(
    ch: Quest, held: int, result: list[RunResult],
    while_waiting: Callable[[], None] = lambda: None,
) -> None:
    with anyio.fail_after(30):
        async with anyio.create_task_group() as tg:
            async def _run() -> None:
                result.append(await run_quest(ch))
            tg.start_soon(_run)
            try:
                await _until(lambda: _persisted_label(ch) == "waiting to test",
                             "the persisted wait label")
                while_waiting()
            finally:
                test_lane.release(held)


def test_free_slot_runs_gate_without_waiting_or_merge_queue(
    tmp_path: Path, provider_registry) -> None:
    _register_fake_provider(provider_registry)
    _seed_control(tmp_path)
    _write_workflow(tmp_path, _gate_step())
    ch = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")

    result = anyio.run(run_quest, ch)

    assert result is RunResult.PAUSED
    types_seen = _types(ch)
    assert "waiting_for_test_lane" not in types_seen   # fast path: no wait status
    assert "test_lane_acquired" not in types_seen
    assert mq.read(tmp_path) == []                      # not routed through the merge queue
    # slot was released when the gate returned
    fd = test_lane.try_acquire(tmp_path, 1)
    assert fd is not None
    test_lane.release(fd)


def test_contended_gate_emits_wait_then_proceeds(
    tmp_path: Path, provider_registry) -> None:
    _register_fake_provider(provider_registry)
    _seed_control(tmp_path)
    _write_workflow(tmp_path, _gate_step())
    ch = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    held = test_lane.try_acquire(tmp_path, 1)            # occupy the sole slot
    assert held is not None
    result: list = []

    async def _scenario() -> None:
        async with anyio.create_task_group() as tg:
            async def _run() -> None:
                result.append(await run_quest(ch))
            tg.start_soon(_run)
            for _ in range(100):
                await anyio.sleep(0.05)
                seen = [e.get("type") for e in JsonlEventLog(ch.events_path).read()]
                if "waiting_for_test_lane" in seen:
                    break
            else:
                raise AssertionError("waiting_for_test_lane was never emitted")
            test_lane.release(held)                      # free the slot; gate proceeds

    anyio.run(_scenario)

    assert result == [RunResult.PAUSED]
    types_seen = [e.get("type") for e in JsonlEventLog(ch.events_path).read()]
    assert "waiting_for_test_lane" in types_seen
    assert "test_lane_acquired" in types_seen            # resolved on acquire


def test_merge_lane_holder_free_slot_emits_no_lane_wait(
    tmp_path: Path, provider_registry, no_daemon_spawn) -> None:
    _register_fake_provider(provider_registry)
    _seed_control(tmp_path)
    _write_workflow(tmp_path, _LANE_STEP + _gate_step())
    ch = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")

    result = anyio.run(run_quest, ch)

    assert result is RunResult.PAUSED
    types_seen = _types(ch)
    assert "lane_acquired" in types_seen
    assert "waiting_for_test_lane" not in types_seen
    assert "test_lane_acquired" not in types_seen


def test_merge_lane_holder_takes_slot_before_waiting_gate(
    tmp_path: Path, provider_registry, no_daemon_spawn, polled: list[str]) -> None:
    _register_fake_provider(provider_registry)
    _seed_control(tmp_path)
    _write_workflow(tmp_path, _LANE_STEP + _gate_step())
    ch = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    held = test_lane.try_acquire(tmp_path, 1)
    assert held is not None
    other_saw_quest_acquire: list[bool] = []

    def _other() -> None:
        fd = test_lane.acquire_blocking(tmp_path, 1)
        other_saw_quest_acquire.append("test_lane_acquired" in _types(ch))
        test_lane.release(fd)

    other = threading.Thread(target=_other, name="other", daemon=True)
    result: list[RunResult] = []

    async def _scenario() -> None:
        with anyio.fail_after(30):
            async with anyio.create_task_group() as tg:
                async def _run() -> None:
                    result.append(await run_quest(ch))
                try:
                    other.start()
                    await _until(lambda: "other" in polled, "the non-priority waiter to wait")
                    tg.start_soon(_run)
                    await _until(lambda: _priority_marker_held(tmp_path),
                                 "the merge-lane holder to wait with priority")
                finally:
                    test_lane.release(held)

    anyio.run(_scenario)
    other.join(10)

    assert result == [RunResult.PAUSED]
    assert other_saw_quest_acquire == [True]
    types_seen = _types(ch)
    assert types_seen.index("waiting_for_test_lane") < types_seen.index("test_lane_acquired")


def test_interruptible_gate_pauses_while_waiting_and_resumes(
    tmp_path: Path, provider_registry, no_daemon_spawn) -> None:
    _register_fake_provider(provider_registry)
    _seed_control(tmp_path)
    ran = tmp_path / "ran"
    _write_workflow(tmp_path, _LANE_STEP + _gate_step(f"touch {ran}", interruptible=True))
    ch = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    coord = InterruptCoordinator()
    held = test_lane.try_acquire(tmp_path, 1)
    assert held is not None
    result: list[RunResult] = []

    async def _interrupt_while_waiting() -> None:
        with anyio.fail_after(30):
            async with anyio.create_task_group() as tg:
                async def _run() -> None:
                    result.append(await run_quest(ch, coordinator=coord))
                tg.start_soon(_run)
                try:
                    await _until(lambda: _priority_marker_held(tmp_path),
                                 "the gate to wait for a slot")
                    coord._should_pause = True
                    coord.pause_reason = "ctrl-c"
                    await _until(lambda: bool(result), "the waiting gate to pause")
                finally:
                    coord._should_pause = True
                    test_lane.release(held)

    anyio.run(_interrupt_while_waiting)

    assert result == [RunResult.PAUSED]
    paused = [e["payload"] for e in JsonlEventLog(ch.events_path).read()
              if e.get("type") == "workflow_paused"]
    assert paused[-1]["reason"] == "interrupt"
    assert not ran.exists()
    assert "test_lane_acquired" not in _types(ch)
    st = State.load(ch.state_dir)
    assert st.step == "run-tests"
    assert st.held_lane == "main" and st.lane_retained is True
    assert mq.is_head(tmp_path, "feat", "main")
    assert not _priority_marker_held(tmp_path)
    held2 = test_lane.try_acquire(tmp_path, 1)
    assert held2 is not None
    resumed: list[RunResult] = []

    async def _resume_then_free_slot() -> None:
        with anyio.fail_after(30):
            async with anyio.create_task_group() as tg:
                async def _run() -> None:
                    resumed.append(await run_quest(ch, coordinator=InterruptCoordinator()))
                tg.start_soon(_run)
                try:
                    await _until(lambda: _types(ch).count("waiting_for_test_lane") == 2
                                 and _priority_marker_held(tmp_path),
                                 "the resumed gate to wait again")
                    assert not ran.exists()
                finally:
                    test_lane.release(held2)

    anyio.run(_resume_then_free_slot)

    assert resumed == [RunResult.PAUSED]
    assert ran.exists()
    assert _types(ch).count("test_lane_acquired") == 1


def test_non_interruptible_gate_keeps_waiting_through_pause_request(
    tmp_path: Path, provider_registry, polled: list[str]) -> None:
    _register_fake_provider(provider_registry)
    _seed_control(tmp_path)
    ran = tmp_path / "ran"
    _write_workflow(tmp_path, _gate_step(f"touch {ran}"))
    ch = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    coord = InterruptCoordinator()
    held = test_lane.try_acquire(tmp_path, 1)
    assert held is not None
    result: list[RunResult] = []

    def _on_event(e: dict) -> None:
        if e.get("type") == "waiting_for_test_lane":
            coord._should_pause = True
            coord.pause_reason = "ctrl-c"

    async def _scenario() -> None:
        with anyio.fail_after(30):
            async with anyio.create_task_group() as tg:
                async def _run() -> None:
                    result.append(await run_quest(ch, on_event=_on_event, coordinator=coord))
                tg.start_soon(_run)
                try:
                    await _until(lambda: coord.should_pause, "the pause request")
                    n = len(polled)
                    await _until(lambda: len(polled) >= n + 3, "the gate to keep polling")
                    assert result == [] and not ran.exists()
                finally:
                    test_lane.release(held)

    anyio.run(_scenario)

    assert result == [RunResult.PAUSED]
    assert ran.exists()
    assert "test_lane_acquired" in _types(ch)


def test_waiting_gate_persists_wait_label_then_restores_run_label(
    tmp_path: Path, provider_registry, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    _register_fake_provider(provider_registry)
    seen = tmp_path / "seen.json"
    ch = _labeled_quest(tmp_path, _snapshot_state(seen))
    held = test_lane.try_acquire(tmp_path, 1)
    assert held is not None
    result: list[RunResult] = []
    boards: list[str] = []

    def _read_board() -> None:
        boards.append("\n".join(m._overview_lines(
            tmp_path, show_all=False, now=datetime.now(UTC), color=False)))

    anyio.run(_run_until_waiting_then_free, ch, held, result, _read_board)

    assert result == [RunResult.PAUSED]
    assert "(integration phase · waiting to test)" in boards[0]
    assert json.loads(seen.read_text())["step_label"] == "running tests"
    assert _persisted_label(ch) == "running tests"


def test_waiting_gate_restores_route_label_not_step_label(
    tmp_path: Path, provider_registry) -> None:
    _register_fake_provider(provider_registry)
    seen = tmp_path / "seen.json"
    ch = _labeled_quest(tmp_path, _snapshot_state(seen))
    st = State.load(ch.state_dir)
    st.route_label = st.step_label = "re-running tests"   # as a labeled goto leaves it
    st.save(ch.state_dir)
    held = test_lane.try_acquire(tmp_path, 1)
    assert held is not None
    result: list[RunResult] = []

    anyio.run(_run_until_waiting_then_free, ch, held, result)

    assert result == [RunResult.PAUSED]
    assert json.loads(seen.read_text())["step_label"] == "re-running tests"
    assert _persisted_label(ch) == "re-running tests"


def test_pause_while_waiting_leaves_run_label_persisted(
    tmp_path: Path, provider_registry) -> None:
    _register_fake_provider(provider_registry)
    ran = tmp_path / "ran"
    ch = _labeled_quest(tmp_path, f"touch {ran}", interruptible=True)
    coord = InterruptCoordinator()
    held = test_lane.try_acquire(tmp_path, 1)
    assert held is not None
    result: list[RunResult] = []

    async def _scenario() -> None:
        with anyio.fail_after(30):
            async with anyio.create_task_group() as tg:
                async def _run() -> None:
                    result.append(await run_quest(ch, coordinator=coord))
                tg.start_soon(_run)
                try:
                    await _until(lambda: _persisted_label(ch) == "waiting to test",
                                 "the persisted wait label")
                    coord._should_pause = True
                    coord.pause_reason = "ctrl-c"
                    await _until(lambda: bool(result), "the waiting gate to pause")
                finally:
                    coord._should_pause = True
                    test_lane.release(held)

    anyio.run(_scenario)

    assert result == [RunResult.PAUSED]
    paused = [e["payload"] for e in JsonlEventLog(ch.events_path).read()
              if e.get("type") == "workflow_paused"]
    assert paused[-1]["reason"] == "interrupt"
    assert not ran.exists()
    assert _persisted_label(ch) == "running tests"


def test_immediate_slot_heals_stale_wait_label_before_script_runs(
    tmp_path: Path, provider_registry) -> None:
    _register_fake_provider(provider_registry)
    seen = tmp_path / "seen.json"
    ch = _labeled_quest(tmp_path, _snapshot_state(seen))
    st = State.load(ch.state_dir)
    st.step_label = "waiting to test"                     # left by a run killed mid-wait
    st.save(ch.state_dir)

    assert anyio.run(run_quest, ch) is RunResult.PAUSED

    assert json.loads(seen.read_text())["step_label"] == "running tests"
    assert _persisted_label(ch) == "running tests"


def test_immediate_slot_never_persists_wait_label(
    tmp_path: Path, provider_registry, monkeypatch: pytest.MonkeyPatch) -> None:
    _register_fake_provider(provider_registry)
    seen = tmp_path / "seen.json"
    ch = _labeled_quest(tmp_path, _snapshot_state(seen))
    saved: list[str | None] = []
    real_save = State.save

    def _spy(self: State, state_dir: Path) -> None:
        saved.append(self.step_label)
        real_save(self, state_dir)
    monkeypatch.setattr(State, "save", _spy)

    assert anyio.run(run_quest, ch) is RunResult.PAUSED

    assert saved and set(saved) == {"running tests"}
    assert json.loads(seen.read_text())["step_label"] == "running tests"
