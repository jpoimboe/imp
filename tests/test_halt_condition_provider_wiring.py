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


@pytest.mark.anyio
async def test_solicited_reply_clears_waiting_halt(tmp_path: Path) -> None:
    from imp.engine.agent_run import _resolve_wait_reply
    from imp.engine.state import InMemoryEventLog
    st = State(id="c1", step="w", step_attempt=0, status="new")
    st.halt_condition = "waiting"
    st.save(tmp_path)

    def _clear_waiting() -> None:
        st.halt_condition = None
        st.save(tmp_path)

    _reply, source = await _resolve_wait_reply(
        None, lambda _p: "go on", False,
        elog=InMemoryEventLog(), agent_id="a1", halt_clearer=_clear_waiting)
    assert source == "solicited"
    assert State.load(tmp_path).halt_condition is None


@pytest.mark.anyio
async def test_nudge_source_does_not_clear_waiting_halt(tmp_path: Path) -> None:
    from imp.engine.agent_run import _resolve_wait_reply
    from imp.engine.state import InMemoryEventLog
    st = State(id="c1", step="w", step_attempt=0, status="new")
    st.halt_condition = "waiting"
    st.save(tmp_path)

    cleared = {"n": 0}

    _reply, source = await _resolve_wait_reply(
        None, lambda _p: "unused", True,
        elog=InMemoryEventLog(), agent_id="a1",
        halt_clearer=lambda: cleared.__setitem__("n", cleared["n"] + 1))
    assert source == "nudge"
    assert cleared["n"] == 0
    assert State.load(tmp_path).halt_condition == "waiting"


@pytest.mark.anyio
async def test_pending_reply_clears_waiting_halt(tmp_path: Path) -> None:
    from imp.engine.agent_run import _resolve_wait_reply
    from imp.engine.state import InMemoryEventLog
    st = State(id="c1", step="w", step_attempt=0, status="new")
    st.halt_condition = "waiting"
    st.save(tmp_path)

    def _clear_waiting() -> None:
        st.halt_condition = None
        st.save(tmp_path)

    reply, source = await _resolve_wait_reply(
        None, lambda _p: "unused", False,
        elog=InMemoryEventLog(), agent_id="a1", pending_reply="resume text",
        halt_clearer=_clear_waiting)
    assert source == "pending_reply"
    assert reply == "resume text"
    assert State.load(tmp_path).halt_condition is None


@pytest.mark.anyio
async def test_interject_reply_clears_waiting_halt(tmp_path: Path) -> None:
    from imp.cli.signals import InterruptCoordinator
    from imp.engine.agent_run import _resolve_wait_reply
    from imp.engine.state import InMemoryEventLog
    st = State(id="c1", step="w", step_attempt=0, status="new")
    st.halt_condition = "waiting"
    st.save(tmp_path)

    coord = InterruptCoordinator()
    coord.enqueue_interject("actually, do X")

    def _clear_waiting() -> None:
        st.halt_condition = None
        st.save(tmp_path)

    reply, source = await _resolve_wait_reply(
        coord, lambda _p: "unused", False,
        elog=InMemoryEventLog(), agent_id="a1", halt_clearer=_clear_waiting)
    assert source == "interject"
    assert reply == "actually, do X"
    assert State.load(tmp_path).halt_condition is None


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
