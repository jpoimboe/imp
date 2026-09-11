from __future__ import annotations

import asyncio
import signal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import anyio
import pytest

from imp.agents.tools.context import ToolContext
from imp.providers.codex_runner import CodexAgentRunner


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(
        project_root=tmp_path,
        events_path=tmp_path / "events.jsonl", subagent_defs={},
        provider_name="codex", model_name="gpt-5.5",
        parent_transcript_dir=tmp_path,
    )


@pytest.mark.anyio
async def test_interrupt_sets_cancel_and_terminates_processes(tmp_path: Path) -> None:
    runner = CodexAgentRunner(model_name="gpt-5.5")
    runner._ctx = _ctx(tmp_path)
    runner._process_owner.acknowledge()

    with patch("imp.providers.codex_runner.terminate_tool_processes",
               new_callable=AsyncMock) as term:
        await runner.interrupt()
        assert runner._cancel is True
        term.assert_awaited_once()
        args, kwargs = term.call_args
        assert args[0] is runner._ctx
        # No timeout literal: interrupt() now relies on the 4 s named grace
        # default (PROCESS_KILL_GRACE_S) inside terminate_tool_processes.
        assert "timeout" not in kwargs
        assert len(args) == 1


@pytest.mark.anyio
async def test_interrupt_is_idempotent(tmp_path: Path) -> None:
    runner = CodexAgentRunner(model_name="gpt-5.5")
    runner._ctx = _ctx(tmp_path)
    runner._process_owner.acknowledge()

    with patch("imp.providers.codex_runner.terminate_tool_processes",
               new_callable=AsyncMock) as term:
        await runner.interrupt()
        await runner.interrupt()  # second call must not raise
        assert term.await_count == 2
    assert runner._cancel is True


@pytest.mark.anyio
async def test_interrupt_waits_for_tool_cleanup_and_owner_acknowledgement(
        tmp_path: Path) -> None:
    runner = CodexAgentRunner(model_name="gpt-5.5")
    runner._ctx = _ctx(tmp_path)
    cleanup_started = anyio.Event()
    cleanup_finished = anyio.Event()
    release_cleanup = anyio.Event()

    async def cleanup(_ctx: ToolContext) -> None:
        cleanup_started.set()
        await release_cleanup.wait()
        cleanup_finished.set()

    with patch("imp.providers.codex_runner.terminate_tool_processes",
               new=AsyncMock(side_effect=cleanup)):
        with anyio.fail_after(120):
            task = asyncio.create_task(runner.interrupt())
            await cleanup_started.wait()
            assert runner._cancel is True and not task.done()
            release_cleanup.set()
            await cleanup_finished.wait()
            assert not task.done()
            runner._process_owner.acknowledge()
            await task

    assert task.done()


@pytest.mark.anyio
async def test_force_kill_cancels_before_signalling_the_owned_process() -> None:
    runner = CodexAgentRunner(model_name="gpt-5.5")
    process = SimpleNamespace(returncode=None, wait=AsyncMock())
    runner._process_owner.publish(process)

    async def kill_tree(proc: object, sig: signal.Signals) -> None:
        assert runner._cancel is True
        assert proc is process and sig == signal.SIGKILL

    with patch("imp.providers.codex_exec.kill_process_tree",
               new=AsyncMock(side_effect=kill_tree)) as kill:
        await runner.force_kill()

    kill.assert_awaited_once_with(process, signal.SIGKILL)
    process.wait.assert_not_awaited()
