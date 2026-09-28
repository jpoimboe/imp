from __future__ import annotations

import signal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import pytest

import imp.agents.tools.context as tool_context
import imp.providers.claude as claude_provider
from imp.engine.state import InMemoryEventLog
from imp.providers.anthropic import AnthropicAgentRunner
from imp.providers.claude import ClaudeAgentRunner
from imp.providers.claude_config_dir import materialize_fresh_config_dir
from imp.providers.openai import OpenAIAgentRunner
from tests.anthropic_fakes import FakeAsyncAnthropic
from tests.async_wait import HANG_BACKSTOP
from tests.cc_fakes import FakeClaudeSDKClient
from tests.openai_fakes import FakeResponsesClient
from tests.test_anthropic_runner import _inv as _anthropic_inv
from tests.test_claude_runner import _inv as _claude_inv
from tests.test_openai_runner import _inv as _openai_inv


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


@pytest.mark.anyio
async def test_openai_leave_cancel_completes_teardown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, torn_down: list[Any],
) -> None:
    entered = anyio.Event()
    fake = FakeResponsesClient([])
    monkeypatch.setattr(fake.responses, "stream", lambda **_kw: _BlockedStream(entered))
    runner = OpenAIAgentRunner(model_name="gpt-4o-mini",
                               client_factory=lambda: fake)
    await _leave_mid_run(runner, _openai_inv(tmp_path), entered)
    assert fake.closed
    assert len(torn_down) == 1


_STDERR_PROBE = "claude: Warning: leave-teardown probe\n"


class _ParkedClaudeClient(FakeClaudeSDKClient):
    """Writes a stderr warning, then parks mid-turn until cancelled."""

    def __init__(self, opts: Any, entered: anyio.Event, process: Any = None) -> None:
        super().__init__(opts, scripted_messages=[])
        self._entered = entered
        self._transport = SimpleNamespace(_process=process)

    async def receive_response(self) -> Any:
        self.opts.stderr(_STDERR_PROBE)
        self._entered.set()
        await anyio.Event().wait()
        yield  # unreachable; makes this an async generator


class _Proc:
    pid = 4242
    reaped = False

    async def wait(self) -> int:
        await anyio.sleep(0)
        self.reaped = True
        return 0


@pytest.mark.anyio
async def test_claude_leave_cancel_completes_teardown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, torn_down: list[Any],
) -> None:
    made: list[Path] = []

    def _materialize() -> Path:
        made.append(materialize_fresh_config_dir())
        return made[-1]

    monkeypatch.setattr(claude_provider, "materialize_fresh_config_dir", _materialize)
    entered = anyio.Event()
    clients: list[_ParkedClaudeClient] = []

    def factory(opts: Any) -> _ParkedClaudeClient:
        clients.append(_ParkedClaudeClient(opts, entered))
        return clients[-1]

    log = InMemoryEventLog()
    runner = ClaudeAgentRunner(model_name="claude-sonnet-4-5", client_factory=factory)
    await _leave_mid_run(runner, _claude_inv(tmp_path, event_log=log), entered)
    fake = clients[0]
    assert fake.closed
    assert runner._client_ctx is None
    warnings = [e["payload"]["message"] for e in log.read() if e["type"] == "runner_warning"]
    assert warnings == ["claude stderr: Warning: leave-teardown probe"]
    assert len(made) == 1
    assert not made[0].exists()
    assert runner._config_dir is None
    assert len(torn_down) == 1
    with anyio.fail_after(HANG_BACKSTOP):
        await runner.interrupt()
    assert fake.interrupts == 0


@pytest.mark.anyio
async def test_claude_leave_cancel_reaps_cut_short_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, torn_down: list[Any],
) -> None:
    signals: list[tuple[int, int]] = []

    async def _record(process: Any, sig: int) -> None:
        signals.append((process.pid, sig))

    monkeypatch.setattr(claude_provider, "kill_process_group", _record)
    entered = anyio.Event()
    proc = _Proc()
    runner = ClaudeAgentRunner(
        model_name="claude-sonnet-4-5",
        client_factory=lambda opts: _ParkedClaudeClient(opts, entered, process=proc))
    await _leave_mid_run(runner, _claude_inv(tmp_path), entered)
    assert signals == [(4242, signal.SIGTERM)]
    assert proc.reaped
    assert len(torn_down) == 1
