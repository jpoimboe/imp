from __future__ import annotations

from typing import Any

import pytest

from imp.cli.signals import InterruptCoordinator


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class RecordingLog:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def append(self, *, agent_id: str | None, type: str,
               payload: dict[str, Any]) -> dict[str, Any]:
        rec = {"agent_id": agent_id, "type": type, "payload": payload}
        self.records.append(rec)
        return rec


@pytest.mark.anyio
async def test_signal_pause_emits_interrupt_requested() -> None:
    coord = InterruptCoordinator()
    log = RecordingLog()
    coord.set_event_log(log)
    await coord.signal_pause(reason="ctrl-c", via="frame")
    reqs = [r for r in log.records if r["type"] == "interrupt_requested"]
    assert len(reqs) == 1
    assert reqs[0]["agent_id"] is None
    assert reqs[0]["payload"] == {"source": "ctrl-c", "via": "frame"}


@pytest.mark.anyio
async def test_interrupt_requested_emitted_once_per_round() -> None:
    coord = InterruptCoordinator()
    log = RecordingLog()
    coord.set_event_log(log)
    await coord.signal_pause(reason="ctrl-c")
    await coord.signal_pause(reason="ctrl-c")  # repeat gesture, same round
    reqs = [r for r in log.records if r["type"] == "interrupt_requested"]
    assert len(reqs) == 1  # first-wins: one requested event per round


@pytest.mark.anyio
async def test_repeat_signal_pause_invokes_runner_once_per_round() -> None:
    coord = InterruptCoordinator()
    calls: list[bool] = []

    async def runner_interrupt() -> None:
        calls.append(True)

    coord.attach_runner(runner_interrupt)
    await coord.signal_pause(reason="ctrl-c")
    await coord.signal_pause(reason="ctrl-c")  # repeat gesture, same round
    assert calls == [True]  # first-wins: runner interrupted once, not per gesture
