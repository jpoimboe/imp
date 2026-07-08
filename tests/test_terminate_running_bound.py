from __future__ import annotations

from typing import Any

import anyio
import pytest

from imp.agents.tools.context import terminate_running_processes


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _HangAfterKill:
    """A child that never reaps: wait() blocks forever even after SIGKILL."""

    def __init__(self) -> None:
        self.pid = None            # pid None -> kill_process_group uses .kill()
        self.returncode = None     # never exits
        self.killed = 0

    def kill(self) -> None:
        self.killed += 1

    def terminate(self) -> None:
        pass

    async def wait(self) -> int:
        await anyio.sleep_forever()
        return 0


class _Ctx:
    def __init__(self, procs: list[Any]) -> None:
        self.running_processes = procs


@pytest.mark.anyio
async def test_terminate_running_is_bounded_when_wait_hangs() -> None:
    # A killed child whose wait() never completes must not hang terminate: the
    # post-SIGKILL reap is bounded by the `timeout` grace. Pass it explicitly small
    # here — the function's default is bound at def-time, so monkeypatching the
    # module global would NOT change the stored default. The point is that
    # terminate returns AT ALL.
    proc = _HangAfterKill()
    ctx = _Ctx([proc])
    with anyio.fail_after(2):
        signalled = await terminate_running_processes(ctx, timeout=0.05)
    assert signalled == [proc]
    assert proc.killed >= 1            # SIGKILL was delivered
    assert ctx.running_processes == []  # registry drained


@pytest.mark.anyio
async def test_anthropic_interrupt_commits_cancel_before_abandonable_awaits(
    monkeypatch: Any,
) -> None:
    """Abandoning interrupt() at the deadline must leave _cancel committed."""
    from imp.providers.anthropic import AnthropicAgentRunner

    runner = AnthropicAgentRunner(model_name="claude-sonnet-4-5")
    # Make the teardown hang so we can abandon interrupt() mid-call.
    hung = anyio.Event()

    async def _hang(_ctx: Any) -> list[Any]:
        await hung.wait()
        return []

    monkeypatch.setattr(
        "imp.agents.tools.context.terminate_running_processes", _hang)

    class _Ctx:
        def __init__(self) -> None:
            self.running_processes: list[Any] = []

    runner._ctx = _Ctx()
    with anyio.move_on_after(0.1):
        await runner.interrupt()
    # Abandoned mid-teardown, but the cancel flag committed first.
    assert runner._cancel is True
    hung.set()


@pytest.mark.anyio
async def test_openai_interrupt_commits_cancel_before_abandonable_awaits(
    monkeypatch: Any,
) -> None:
    """openai mirrors anthropic: _cancel set before the teardown awaits."""
    from imp.providers.openai import OpenAIAgentRunner

    runner = OpenAIAgentRunner(model_name="gpt-5")
    hung = anyio.Event()

    async def _hang(_ctx: Any) -> list[Any]:
        await hung.wait()
        return []

    monkeypatch.setattr(
        "imp.agents.tools.context.terminate_running_processes", _hang)

    class _Ctx:
        def __init__(self) -> None:
            self.running_processes: list[Any] = []

    runner._ctx = _Ctx()
    with anyio.move_on_after(0.1):
        await runner.interrupt()
    assert runner._cancel is True
    hung.set()


@pytest.mark.anyio
async def test_codex_interrupt_commits_cancel_before_abandonable_awaits(
    monkeypatch: Any,
) -> None:
    """codex sets _cancel before the teardown; abandoning mid-call keeps it committed."""
    from imp.providers.codex_runner import CodexAgentRunner

    runner = CodexAgentRunner(model_name="gpt-5-codex")
    hung = anyio.Event()

    async def _hang(_ctx: Any) -> list[Any]:
        await hung.wait()
        return []

    # codex_runner binds terminate_running_processes at module top: patch that module, not context.
    monkeypatch.setattr(
        "imp.providers.codex_runner.terminate_running_processes", _hang)

    class _Ctx:
        def __init__(self) -> None:
            self.running_processes: list[Any] = []

    runner._ctx = _Ctx()
    runner._abort_controller = None  # no SDK stream in this unit test
    with anyio.move_on_after(0.1):
        await runner.interrupt()
    assert runner._cancel is True
    hung.set()
