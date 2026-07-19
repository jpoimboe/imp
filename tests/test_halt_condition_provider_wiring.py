from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from imp.engine.state import State


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_invocation_halt_writer_marks_waiting_through_resolve(
    tmp_path: Path,
) -> None:
    from imp.engine.agent_run import _resolve_wait_reply
    from imp.engine.state import InMemoryEventLog
    st = State(id="c1", step="w", step_attempt=0, status="new")
    st.save(tmp_path)

    def _mark_waiting() -> None:
        st.halt_condition = "waiting"
        st.save(tmp_path)

    # The exact shape the providers use: pass the writer positionally-by-keyword.
    _reply, source = await _resolve_wait_reply(
        None, lambda _p: "hi", False,
        elog=InMemoryEventLog(), agent_id="a1", halt_writer=_mark_waiting)
    assert source == "solicited"
    assert State.load(tmp_path).halt_condition == "waiting"


def test_every_provider_call_forwards_halt_writer() -> None:
    # Structural guard: each provider's _resolve_wait_reply call site and the
    # claude-code steer must reference invocation.halt_writer, so a dropped
    # forward is caught without a full provider round-trip.
    import imp.providers.anthropic as a
    import imp.providers.claude_code as c
    import imp.providers.codex_runner as cx
    import imp.providers.openai as o
    for mod in (a, o, cx, c):
        assert "invocation.halt_writer" in inspect.getsource(mod), mod.__name__
