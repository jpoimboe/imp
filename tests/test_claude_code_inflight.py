# tests/test_claude_code_inflight.py
from __future__ import annotations

import asyncio
import json

import pytest

from imp.providers.claude_code import (
    INFLIGHT_RESULT_BOUND_S,
    _RunState,
    _settle_inflight,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_settle_reports_the_real_result_of_a_late_finishing_call() -> None:
    """The point of the bound: a call still running when the interrupt landed is
    waited out and reports what it actually did, not what the SDK guessed. The wait
    is the full production bound, so a loaded machine simply waits longer."""
    state = _RunState()
    state.register_inflight("k1", "write_file")

    async def _finish_late() -> None:
        await asyncio.sleep(0.05)
        state.resolve_inflight("k1", "wrote 12 bytes to notes.md", is_error=False)

    task = asyncio.create_task(_finish_late())
    settled = await _settle_inflight(state, INFLIGHT_RESULT_BOUND_S)
    await task

    assert [(s.key, s.name, s.content, s.is_error) for s in settled] == [
        ("k1", "write_file", "wrote 12 bytes to notes.md", False)]
    assert state.inflight == {}


@pytest.mark.anyio
async def test_settle_substitutes_the_uncertainty_body_when_the_bound_expires(
) -> None:
    """A wedged call yields an honest statement of uncertainty, per tool shape —
    never a claim of rejection, and never a fabricated returncode."""
    state = _RunState()
    state.register_inflight("k1", "write_file")
    state.register_inflight("k2", "bash")

    settled = {s.key: s for s in await _settle_inflight(state, 0.05)}

    assert settled["k1"].content == (
        "imp interrupted this turn; this call was still running and its outcome "
        "was not recorded. Verify the current state before relying on it.")
    assert settled["k1"].is_error is True
    assert json.loads(settled["k2"].content) == {
        "stdout": "",
        "stderr": settled["k1"].content,
        "returncode": None,
    }
    assert settled["k2"].is_error is True


@pytest.mark.anyio
async def test_settle_returns_immediately_when_nothing_is_in_flight() -> None:
    """The common case pays nothing — stalling an ordinary interrupt for the bound
    would be the regression this guards."""
    state = _RunState()
    assert await asyncio.wait_for(
        _settle_inflight(state, INFLIGHT_RESULT_BOUND_S), timeout=10) == []
