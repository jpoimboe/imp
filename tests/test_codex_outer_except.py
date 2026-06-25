from __future__ import annotations

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

    events = [ev async for ev in runner.run(object())]  # type: ignore[arg-type]

    assert len(events) == 1
    assert events[0]["type"] == "error"
    assert events[0]["payload"]["category"] == "unknown"


@pytest.mark.anyio
async def test_outer_except_reraises_subagent_suspended() -> None:
    runner = _runner_with_failing_loop(SubagentSuspended("paused"))
    runner._cancel = False

    with pytest.raises(SubagentSuspended):
        async for _ev in runner.run(object()):  # type: ignore[arg-type]
            pass


@pytest.mark.anyio
async def test_outer_except_reraises_awaiting_user_input() -> None:
    from imp.agents.tools.wait_for_user import AwaitingUserInput

    runner = _runner_with_failing_loop(AwaitingUserInput("the question"))
    runner._cancel = False

    with pytest.raises(AwaitingUserInput):
        async for _ev in runner.run(object()):  # type: ignore[arg-type]
            pass
