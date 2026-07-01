from pathlib import Path

import anyio

from imp.engine import merge_queue as mq
from imp.engine import test_lane
from imp.engine.change import Change
from imp.engine.executor import RunResult, run_change
from imp.engine.state import JsonlEventLog
from tests.conftest import _git, _seed_control


def _write_testlane_workflow(root: Path) -> None:
    wf = root / "imp" / "workflows" / "imp" / "workflow.yaml"
    wf.write_text(
        "name: t\ndescription: \"\"\n"
        "phases:\n"
        "  - name: gate\n"
        "    steps:\n"
        "      - name: run-tests\n"
        "        command: \"true\"\n"
        "        acquire: test-lane\n"
        "        rules:\n"
        "          - {if: {exit: 0}, pause: true}\n"
        "          - {else: true, pause: true}\n"
    )
    (root / ".imp" / "config.yaml").write_text(
        (root / ".imp" / "config.yaml").read_text()
        + "sandbox:\n  enabled: false\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "test-lane workflow")


def _register_fake_provider(provider_registry) -> None:
    from tests.test_executor_rename import _FakeProvider
    provider_registry("anthropic", lambda: True, lambda model: _FakeProvider(model))


def test_free_slot_runs_gate_without_waiting_or_merge_queue(
    tmp_path: Path, provider_registry) -> None:
    _register_fake_provider(provider_registry)
    _seed_control(tmp_path)
    _write_testlane_workflow(tmp_path)
    ch = Change.create(tmp_path, title="x", change_id="feat", base_branch="main")

    result = anyio.run(run_change, ch)

    assert result is RunResult.PAUSED
    types_seen = [e.get("type") for e in JsonlEventLog(ch.events_path).read()]
    assert "waiting_for_test_lane" not in types_seen   # fast path: no wait status
    assert mq.read(tmp_path) == []                      # not routed through the merge queue
    # slot was released when the gate returned
    fd = test_lane.try_acquire(tmp_path, 1)
    assert fd is not None
    test_lane.release(fd)


def test_contended_gate_emits_wait_then_proceeds(
    tmp_path: Path, provider_registry) -> None:
    _register_fake_provider(provider_registry)
    _seed_control(tmp_path)
    _write_testlane_workflow(tmp_path)
    ch = Change.create(tmp_path, title="x", change_id="feat", base_branch="main")
    held = test_lane.try_acquire(tmp_path, 1)            # occupy the sole slot
    assert held is not None
    result: list = []

    async def _scenario() -> None:
        async with anyio.create_task_group() as tg:
            async def _run() -> None:
                result.append(await run_change(ch))
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
