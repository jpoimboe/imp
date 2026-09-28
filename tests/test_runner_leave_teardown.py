from __future__ import annotations

from pathlib import Path
from typing import Any

import anyio
import pytest

import imp.agents.tools.context as tool_context
from imp.providers.anthropic import AnthropicAgentRunner
from tests.anthropic_fakes import FakeAsyncAnthropic
from tests.async_wait import HANG_BACKSTOP
from tests.test_anthropic_runner import _inv as _anthropic_inv


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _BlockedStream:
    """A provider stream that parks the run mid-request until cancelled."""

    def __init__(self, entered: anyio.Event) -> None:
        self._entered = entered

    async def __aenter__(self) -> None:
        self._entered.set()
        await anyio.Event().wait()

    async def __aexit__(self, *_exc: Any) -> bool:
        return False


@pytest.fixture
def torn_down(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Spy teardown_tool_context; it records only if its own await survives."""
    done: list[Any] = []

    async def spy(ctx: Any) -> None:
        await anyio.sleep(0)
        done.append(ctx)

    monkeypatch.setattr(tool_context, "teardown_tool_context", spy)
    return done


async def _leave_mid_run(runner: Any, inv: Any, entered: anyio.Event) -> None:
    """Cancel the run's scope from another task, as the session server's leave controls do."""
    run_scope = anyio.CancelScope()

    async def _drive() -> None:
        with run_scope:
            async for _ev in runner.run(inv):
                pass

    with anyio.fail_after(HANG_BACKSTOP):
        async with anyio.create_task_group() as tg:
            tg.start_soon(_drive)
            await entered.wait()
            run_scope.cancel()
    assert run_scope.cancelled_caught


@pytest.mark.anyio
async def test_anthropic_leave_cancel_completes_teardown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, torn_down: list[Any],
) -> None:
    entered = anyio.Event()
    fake = FakeAsyncAnthropic([])
    monkeypatch.setattr(fake.messages, "stream", lambda **_kw: _BlockedStream(entered))
    runner = AnthropicAgentRunner(model_name="claude-sonnet-4-6",
                                  client_factory=lambda: fake)
    await _leave_mid_run(runner, _anthropic_inv(tmp_path), entered)
    assert fake.closed
    assert len(torn_down) == 1
