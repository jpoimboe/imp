from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

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
async def test_interrupt_sets_cancel_terminates_processes_and_aborts_sdk(tmp_path: Path) -> None:
    runner = CodexAgentRunner(model_name="gpt-5.5")
    runner._ctx = _ctx(tmp_path)

    class _Abort:
        def __init__(self) -> None:
            self.calls = 0
        def abort(self) -> None:
            self.calls += 1

    ab = _Abort()
    runner._abort_controller = ab

    with patch("imp.providers.codex_runner.terminate_running_processes",
               new_callable=AsyncMock) as term:
        await runner.interrupt()
        assert runner._cancel is True
        term.assert_awaited_once()
        args, kwargs = term.call_args
        assert args[0] is runner._ctx
        # No timeout literal: interrupt() now relies on the 4 s named grace
        # default (PROCESS_KILL_GRACE_S) inside terminate_running_processes.
        assert "timeout" not in kwargs
        assert len(args) == 1
        assert ab.calls == 1


@pytest.mark.anyio
async def test_interrupt_is_idempotent(tmp_path: Path) -> None:
    runner = CodexAgentRunner(model_name="gpt-5.5")
    runner._ctx = _ctx(tmp_path)

    class _Abort:
        def __init__(self) -> None:
            self.calls = 0
        def abort(self) -> None:
            self.calls += 1

    ab = _Abort()
    runner._abort_controller = ab
    with patch("imp.providers.codex_runner.terminate_running_processes",
               new_callable=AsyncMock):
        await runner.interrupt()
        await runner.interrupt()  # second call must not raise
    assert runner._cancel is True
    assert ab.calls >= 1
