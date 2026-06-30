# tests/test_drive_close.py
"""drive_invocation must aclose() the runner generator on EVERY exit — error
events, pause, SubagentSuspended/AwaitingUserInput — so run()'s finally
(client close + background teardown) runs NOW, before any re-drive, not at
nondeterministic GC finalization. (spec: Teardown site 3)"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from imp.engine.agent_run import drive_invocation
from tests.test_heartbeat import _inv, _PauseCoordinator  # reuse helpers


@pytest.fixture
def anyio_backend():
    return "asyncio"


class _ClosableRunner:
    """Yields a scripted event list; records whether its generator's finally
    ran (the stand-in for run()'s client-close + bg-teardown finally)."""

    def __init__(self, events: list[dict[str, Any]],
                 raise_after: Exception | None = None) -> None:
        self._events = events
        self._raise_after = raise_after
        self.closed = False
        self.interrupted = False

    async def interrupt(self) -> None:
        self.interrupted = True

    def run(self, invocation: Any):
        async def gen():
            try:
                for ev in self._events:
                    yield ev
                if self._raise_after is not None:
                    raise self._raise_after
            finally:
                self.closed = True
        return gen()


@pytest.mark.anyio
async def test_gen_closed_on_error_event_return(tmp_path: Path) -> None:
    """The error/backoff path returns from inside the async for — the finally
    must still close the generator before drive_invocation returns."""
    runner = _ClosableRunner([{
        "type": "error",
        "payload": {"category": "network", "kind": "x", "message": "boom"},
    }])
    result = await drive_invocation(
        _inv(tmp_path, {"complete_step"}), runner,
        coordinator=None, log=None, emit_events=False)
    assert result.errored is True
    assert runner.closed is True


@pytest.mark.anyio
async def test_gen_closed_on_pause_return(tmp_path: Path) -> None:
    runner = _ClosableRunner([
        {"type": "tool_heartbeat", "payload": {}},
        {"type": "agent_output", "payload": {"status": "Success"}},
    ])
    result = await drive_invocation(
        _inv(tmp_path, {"complete_step"}), runner,
        coordinator=_PauseCoordinator(), log=None, emit_events=False)
    assert result.paused is True
    assert runner.closed is True


@pytest.mark.anyio
async def test_gen_closed_on_subagent_suspended(tmp_path: Path) -> None:
    from imp.engine.agent_run import SubagentSuspended
    runner = _ClosableRunner([], raise_after=SubagentSuspended())
    result = await drive_invocation(
        _inv(tmp_path, {"complete_step"}), runner,
        coordinator=None, log=None, emit_events=False)
    assert result.paused is True
    assert runner.closed is True


class _NoPauseCoordinator(_PauseCoordinator):
    """Cooperative-steer already cleared the pause: should_pause is False."""
    should_pause = False


@pytest.mark.anyio
async def test_interrupted_error_pauses_with_interrupt_flag_when_pause_cleared(
    tmp_path: Path,
) -> None:
    """A claude_code mid-tool Ctrl-C can surface as an `interrupted` error event
    AFTER the cooperative steer cleared should_pause. It must still pause with
    interrupt=True (not classify as a fatal `config` pause) so the resume
    continues the step. This is the gate-only path the -n4 live load exposed."""
    runner = _ClosableRunner([{
        "type": "error",
        "payload": {"category": "config", "kind": "interrupted",
                    "message": "runner cancelled by interrupt"},
    }])
    result = await drive_invocation(
        _inv(tmp_path, {"complete_step"}), runner,
        coordinator=_NoPauseCoordinator(), log=None, emit_events=False)
    assert result.paused is True
    assert result.fatal is False
    assert result.interrupt is True
    assert runner.closed is True


@pytest.mark.anyio
async def test_should_pause_error_pauses_with_interrupt_flag(
    tmp_path: Path,
) -> None:
    """An error event arriving with should_pause already set is an interrupt in
    flight: pause with interrupt=True regardless of the error's own category."""
    runner = _ClosableRunner([{
        "type": "error",
        "payload": {"category": "network", "kind": "x", "message": "boom"},
    }])
    result = await drive_invocation(
        _inv(tmp_path, {"complete_step"}), runner,
        coordinator=_PauseCoordinator(), log=None, emit_events=False)
    assert result.paused is True
    assert result.interrupt is True
    assert runner.closed is True


@pytest.mark.anyio
async def test_non_interrupt_error_stays_fatal_without_interrupt_flag(
    tmp_path: Path,
) -> None:
    """A genuine non-interrupt error (no should_pause, not interrupted-kind) is
    still classified normally and never spuriously marked interrupt=True."""
    runner = _ClosableRunner([{
        "type": "error",
        "payload": {"category": "config", "kind": "bad_request",
                    "message": "boom"},
    }])
    result = await drive_invocation(
        _inv(tmp_path, {"complete_step"}), runner,
        coordinator=_NoPauseCoordinator(), log=None, emit_events=False)
    assert result.errored is True
    assert result.fatal is True
    assert result.interrupt is False
    assert runner.closed is True


@pytest.mark.anyio
async def test_gen_closed_on_success(tmp_path: Path) -> None:
    runner = _ClosableRunner([
        {"type": "agent_output", "payload": {"status": "Success"}},
    ])
    result = await drive_invocation(
        _inv(tmp_path, {"complete_step"}), runner,
        coordinator=None, log=None, emit_events=False)
    assert result.output == {"status": "Success"}
    assert runner.closed is True
