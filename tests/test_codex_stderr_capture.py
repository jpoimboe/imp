# tests/test_codex_stderr_capture.py
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from imp.providers.codex_runner import CodexAgentRunner
from imp.providers.stderr_sink import StderrSink, current_stderr_sink


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_codex_failure_floor_attaches_stderr_tail(tmp_path: Path) -> None:
    runner = CodexAgentRunner(model_name="gpt-5.5")

    async def _boom(_invocation: Any):  # async generator that publishes a sink, then dies
        sink = StderrSink(tmp_path / "stderr.log")
        runner._stderr_sink = sink
        token = current_stderr_sink.set(sink)
        try:
            if False:
                yield {}
            raise RuntimeError(
                "Codex Exec exited with code 1: codex: fatal: simulated boom")
        finally:
            current_stderr_sink.reset(token)

    runner._run_loop = _boom  # type: ignore[assignment]
    runner._cancel = False

    events = [ev async for ev in runner.run(object())]  # type: ignore[arg-type]

    assert len(events) == 1
    payload = events[0]["payload"]
    assert payload["category"] == "unknown"
    assert "simulated boom" in str(payload["detail"].get("stderr_tail"))


@pytest.mark.anyio
async def test_codex_floor_prefers_live_capture_without_duplicating(
    tmp_path: Path,
) -> None:
    """When the streaming tee already captured the CLI's stderr, the floor must
    NOT re-feed str(exc): the CodexExecError message re-embeds that same stderr
    and already lands in the event's `message`. The tail stays the live
    capture, unduplicated."""
    runner = CodexAgentRunner(model_name="gpt-5.5")

    async def _boom(_invocation: Any):
        sink = StderrSink(tmp_path / "stderr.log")
        sink.feed("live: codex stderr line\n")  # the tee already captured this
        runner._stderr_sink = sink
        token = current_stderr_sink.set(sink)
        try:
            if False:
                yield {}
            raise RuntimeError(
                "Codex Exec exited with code 1: live: codex stderr line")
        finally:
            current_stderr_sink.reset(token)

    runner._run_loop = _boom  # type: ignore[assignment]
    runner._cancel = False

    events = [ev async for ev in runner.run(object())]  # type: ignore[arg-type]

    tail = events[0]["payload"]["detail"]["stderr_tail"]
    assert tail == "live: codex stderr line\n"  # exc message NOT appended
