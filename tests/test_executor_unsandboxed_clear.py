from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from imp.cli.signals import InterruptCoordinator
from imp.engine.executor import RunResult, run_quest
from imp.engine.state import State
from tests.test_executor_retry_continue import (
    _A,
    _SINGLE_WF,
    _TWO_STEP_WF,
    _WRITER,
    _make_change,
    _SuccessProvider,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_advance_to_different_step_clears_override_and_broadcasts_marker(
    tmp_path: Path, provider_registry: Any,
) -> None:
    quest = _make_change(tmp_path, agents={"a.md": _A}, workflow_yaml=_TWO_STEP_WF)
    # Enter with the override live on both the coordinator and persisted State.
    s = State.load(quest.state_dir)
    s.unsandboxed = True
    s.save(quest.state_dir)
    coord = InterruptCoordinator()
    coord.set_unsandboxed(True)
    provider_registry("fake", env_check=lambda: True,
                      factory=lambda _m: _SuccessProvider())

    events: list[dict] = []
    result = await run_quest(quest, on_event=events.append, coordinator=coord)

    assert result is RunResult.COMPLETED
    # The s1 -> s2 advance retired the override on both substrates.
    assert State.load(quest.state_dir).unsandboxed is False
    assert coord.unsandboxed is False
    # A transient sandbox_marker {unsandboxed: false} was broadcast via on_event...
    markers = [e for e in events if e.get("type") == "sandbox_marker"]
    assert markers and markers[-1]["payload"] == {"unsandboxed": False}
    # ...but never persisted to the event log (it is a live-only UI mirror).
    assert "sandbox_marker" not in quest.events_path.read_text()


@pytest.mark.anyio
async def test_same_step_pause_and_resume_preserves_override(
    tmp_path: Path, provider_registry: Any,
) -> None:
    quest = _make_change(tmp_path, agents={"writer.md": _WRITER},
                         workflow_yaml=_SINGLE_WF)
    s = State.load(quest.state_dir)
    s.unsandboxed = True
    s.save(quest.state_dir)
    coord = InterruptCoordinator()
    coord.set_unsandboxed(True)

    class _InterruptingRunner:
        async def run(self, invocation: Any) -> Any:
            await coord.signal_pause(reason="ctrl-c")
            yield {"type": "agent_output",
                   "payload": {"outcome": "ok", "status": "Success"}}

        async def interrupt(self) -> None:
            pass

    class _P:
        name = "fake"
        model = "m"

        def create_runner(self, _inv: Any) -> _InterruptingRunner:
            return _InterruptingRunner()

    provider_registry("fake", env_check=lambda: True, factory=lambda _m: _P())

    events: list[dict] = []
    result = await run_quest(quest, on_event=events.append, coordinator=coord)

    assert result is RunResult.PAUSED
    # A Pause returns before the step-advance choke point, so the override survives
    # a same-step pause/resume and no clear-marker is broadcast.
    assert State.load(quest.state_dir).unsandboxed is True
    assert coord.unsandboxed is True
    assert not any(e.get("type") == "sandbox_marker" for e in events)
