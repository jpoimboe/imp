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
    # Structural guard: the wait-reply resolution must forward invocation.halt_writer.
    # anthropic/openai share the PushSession base (their forwarding lives there once);
    # codex_runner and claude forward directly.
    import imp.providers.claude as c
    import imp.providers.codex_runner as cx
    import imp.providers.push_session as ps
    for mod in (ps, cx, c):
        assert "invocation.halt_writer" in inspect.getsource(mod), mod.__name__


def test_agent_invocation_has_halt_clearer_field() -> None:
    from dataclasses import fields

    from imp.agents.runner import AgentInvocation
    names = {f.name for f in fields(AgentInvocation)}
    assert "halt_clearer" in names


def test_every_provider_call_forwards_halt_clearer() -> None:
    # Structural guard: the wait-reply resolution must forward invocation.halt_clearer,
    # mirroring the halt_writer guard, so a dropped forward is caught without a full round-trip.
    # anthropic/openai share the PushSession base (their forwarding lives there once);
    # codex_runner and claude forward directly.
    import imp.providers.claude as c
    import imp.providers.codex_runner as cx
    import imp.providers.push_session as ps
    for mod in (ps, cx, c):
        assert "invocation.halt_clearer" in inspect.getsource(mod), mod.__name__
