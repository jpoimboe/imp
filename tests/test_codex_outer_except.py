from __future__ import annotations

import anyio
import pytest

from imp.engine.agent_run import SubagentSuspended
from imp.providers.codex_runner import CodexAgentRunner


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _runner_with_failing_loop(exc: BaseException) -> CodexAgentRunner:
    runner = CodexAgentRunner(model_name="gpt-5.5")

    async def _boom(_invocation):  # shadows the bound method; not a coroutine-method
        if False:  # make this an async generator
            yield {}
        raise exc

    runner._run_loop = _boom  # type: ignore[assignment]
    return runner


@pytest.mark.anyio
async def test_outer_except_reclassifies_to_interrupted_when_cancel_set() -> None:
    runner = _runner_with_failing_loop(RuntimeError("transport torn down"))
    runner._cancel = True

    events = [ev async for ev in runner.run(object())]  # type: ignore[arg-type]

    assert len(events) == 1
    assert events[0] == {"type": "error", "payload": {
        "category": "config", "kind": "interrupted",
        "message": "runner cancelled by interrupt"}}


@pytest.mark.anyio
async def test_outer_except_unknown_when_cancel_unset() -> None:
    runner = _runner_with_failing_loop(RuntimeError("genuine bug"))
    runner._cancel = False
    runner._interrupt_grace_s = 0.01  # genuine error: keep the grace-wait short

    events = [ev async for ev in runner.run(object())]  # type: ignore[arg-type]

    assert len(events) == 1
    assert events[0]["type"] == "error"
    assert events[0]["payload"]["category"] == "unknown"


@pytest.mark.anyio
async def test_outer_except_reclassifies_when_interrupt_lands_during_grace() -> None:
    """The process-group Ctrl-C race: the native `codex exec` dies and the SDK
    raises a CodexExecError into the outer except *before* the async signal
    chain (run_signal_receiver -> signal_pause -> interrupt()) has set
    `self._cancel`. The interrupt is already pending; it lands a few event-loop
    turns later. The runner must yield the loop briefly and reclassify the
    error as a clean interrupt — NOT `category:"unknown"` (the §0 bar)."""
    runner = _runner_with_failing_loop(
        RuntimeError("Codex Exec exited with code 1: Reading prompt from stdin..."))
    runner._cancel = False  # not yet propagated when the SDK error surfaces

    async def _delayed_cancel() -> None:
        # Simulates the async signal receiver flipping the flag a few turns
        # after the exception reaches the outer except.
        await anyio.sleep(0.01)
        runner._cancel = True

    async with anyio.create_task_group() as tg:
        tg.start_soon(_delayed_cancel)
        events = [ev async for ev in runner.run(object())]  # type: ignore[arg-type]

    assert len(events) == 1
    assert events[0] == {"type": "error", "payload": {
        "category": "config", "kind": "interrupted",
        "message": "runner cancelled by interrupt"}}


@pytest.mark.anyio
async def test_outer_except_reraises_subagent_suspended() -> None:
    runner = _runner_with_failing_loop(SubagentSuspended("paused"))
    runner._cancel = False

    with pytest.raises(SubagentSuspended):
        async for _ev in runner.run(object()):  # type: ignore[arg-type]
            pass
