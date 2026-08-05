"""imp.api.Session: the async-iterator event stream and client-side gate tracking."""

from __future__ import annotations

import anyio
import pytest

from imp.api import (
    DaemonUnreachable,
    Event,
    ImpError,
    NotAtGate,
    Session,
    _Feed,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _FakeClient:
    """Stands in for SessionClient: records what reached the socket, if anything."""

    def __init__(self) -> None:
        self.inputs: list[str] = []
        self.controls: list[tuple[str, str | None]] = []
        self.closed = 0

    async def send_input(self, text: str) -> None:
        self.inputs.append(text)

    async def send_control(self, action: str, reason: str | None = None) -> None:
        self.controls.append((action, reason))

    async def aclose(self) -> None:
        self.closed += 1


@pytest.mark.anyio
async def test_events_yields_every_frame_in_order_including_unknown_types() -> None:
    feed = _Feed()
    session = Session(_FakeClient(), feed)
    for raw in (
        {"id": "e1", "agent_id": "a1", "type": "step_started",
         "payload": {"step": "plan"}},
        {"id": "e2", "type": "weather_report", "payload": {"sky": "clear"}},
        {"type": "replay_complete"},
    ):
        feed.on_event(raw)
    feed.close()

    got = [event async for event in session.events()]

    # An unknown type rides the stream like any other; nothing raises. (AC 10b)
    assert [e.type for e in got] == [
        "step_started", "weather_report", "replay_complete"]
    assert isinstance(got[0], Event)
    assert got[0].payload == {"step": "plan"}
    assert got[0].raw["agent_id"] == "a1"      # the full envelope, not just the payload
    assert got[2].payload == {}                # a boundary frame carries no payload


@pytest.mark.anyio
async def test_events_ends_cleanly_when_the_context_closes_under_a_reader() -> None:
    # attach()'s teardown closes the receive half so no stream is left for the GC.
    # A consumer streaming in another task must see that as end-of-stream, not a crash.
    feed = _Feed()
    session = Session(_FakeClient(), feed)
    feed.on_event({"type": "step_started", "payload": {}})
    feed.recv.close()

    assert [event async for event in session.events()] == []


@pytest.mark.anyio
async def test_send_input_with_no_gate_open_raises_not_at_gate() -> None:
    client = _FakeClient()
    session = Session(client, _Feed())

    with pytest.raises(NotAtGate):
        await session.send_input("us")

    assert client.inputs == []                 # raised locally, never touched the socket


@pytest.mark.anyio
async def test_a_live_gate_opens_the_window_and_resolution_closes_it() -> None:
    feed = _Feed()
    client = _FakeClient()
    session = Session(client, feed)

    # Note the consumer has drained nothing: the gate is open because the frame
    # ARRIVED. Tracking on iteration instead would reject this valid reply.
    feed.on_event({"type": "awaiting_user_input", "payload": {"prompt": "?"}})
    await session.send_input("us")
    assert client.inputs == ["us"]

    feed.on_event({"type": "user_input_resolved", "payload": {}})
    with pytest.raises(NotAtGate):
        await session.send_input("again")
    assert client.inputs == ["us"]


@pytest.mark.anyio
async def test_a_replayed_gate_does_not_open_the_window() -> None:
    feed = _Feed()
    client = _FakeClient()
    session = Session(client, feed)

    feed.on_event({"type": "awaiting_user_input", "payload": {"prompt": "?"},
                   "_replay": True})

    with pytest.raises(NotAtGate):
        await session.send_input("us")
    assert client.inputs == []


@pytest.mark.anyio
async def test_interrupt_sends_the_control_frame_the_terminal_driver_sends() -> None:
    client = _FakeClient()
    session = Session(client, _Feed())

    await session.interrupt()
    await session.interrupt("stop that")

    assert client.controls == [("interrupt", None), ("interrupt", "stop that")]


@pytest.mark.anyio
async def test_a_dead_daemon_surfaces_as_daemon_unreachable_not_a_raw_anyio_error(
) -> None:
    # A daemon that exits or hibernates mid-session breaks the socket under both
    # senders. Scope 1's whole point is that a consumer catches ImpError.
    class _DeadClient(_FakeClient):
        async def send_input(self, text: str) -> None:
            raise anyio.BrokenResourceError

        async def send_control(self, action: str, reason: str | None = None) -> None:
            raise anyio.ClosedResourceError

    feed = _Feed()
    feed.on_event({"type": "awaiting_user_input", "payload": {}})
    session = Session(_DeadClient(), feed)

    with pytest.raises(DaemonUnreachable) as sent:
        await session.send_input("us")
    with pytest.raises(DaemonUnreachable) as interrupted:
        await session.interrupt()

    # anyio's stream errors stringify to "", so the message must be ours, not wrapped.
    assert "hibernated" in str(sent.value)
    assert isinstance(interrupted.value, ImpError)


@pytest.mark.anyio
async def test_aclose_closes_the_underlying_client_and_repeats_harmlessly() -> None:
    client = _FakeClient()
    session = Session(client, _Feed())

    await session.aclose()
    await session.aclose()

    assert client.closed == 2
