from __future__ import annotations

from pathlib import Path
from typing import Any

import anyio
import pytest

from imp.remote.session.relay import SessionRelay
from imp.remote.session.socket_server import SessionSocket
from tests.async_wait import wait_until


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_relay_forwards_curated_events_with_quest_context(
    tmp_path: Path,
) -> None:
    sock = str(tmp_path / "s.sock")
    socket = SessionSocket(sock)
    await socket.open()
    sink: list[dict[str, Any]] = []
    relay = SessionRelay(
        socket_path=sock, quest_id="cid", title_provider=lambda: "T",
        events=("all",), sink=sink.append)
    async with anyio.create_task_group() as tg:
        tg.start_soon(socket.serve)
        tg.start_soon(relay.run)
        await wait_until(lambda: socket.attached_count != 0)
        socket.broadcast(
            {"id": "e1", "type": "agent_result", "payload": {"text": "hi"}})
        await wait_until(lambda: sink)
        tg.cancel_scope.cancel()
    await socket.close()
    assert sink[0]["id"] == "e1"
    assert sink[0]["type"] == "agent_result"
    assert sink[0]["quest"] == {
        "id": "cid", "title": "T", "step": None, "status": "running"}


@pytest.mark.anyio
async def test_relay_stays_attached_at_gate_and_drops_lease(tmp_path: Path) -> None:
    # A live gate drops the lease but NOT the attachment: the relay stays attached
    # for the session's whole residency and reflects awaiting-ness in its lease.
    sock = str(tmp_path / "s.sock")
    socket = SessionSocket(sock)
    await socket.open()
    sink: list[dict[str, Any]] = []
    relay = SessionRelay(
        socket_path=sock, quest_id="cid", title_provider=lambda: "T", events=("all",),
        sink=sink.append, is_subscribed=lambda: True, lease_poll_gap=0.02)
    async with anyio.create_task_group() as tg:
        tg.start_soon(socket.serve)
        tg.start_soon(relay.run)
        await wait_until(lambda: socket.leased_count != 0)
        assert socket.attached_count == 1
        socket.broadcast(
            {"type": "awaiting_user_input", "payload": {"prompt": "go?"}})
        # Gate drops the lease within one poll gap.
        await wait_until(lambda: socket.leased_count == 0)
        # ...but the relay is still attached (would have been 0 under detach-at-gate).
        assert socket.attached_count == 1
        assert sink[-1]["type"] == "awaiting_user_input"
        tg.cancel_scope.cancel()
    await socket.close()


@pytest.mark.anyio
async def test_relay_awaiting_then_live_events_reach_sink_and_toggle_lease(
    tmp_path: Path,
) -> None:
    # Incident regression: a live gate followed by two live events all reach the
    # sink, and a set-lease false / set-lease true pair crosses the wire
    # (leased_count 1 -> 0 -> 1).
    sock = str(tmp_path / "s.sock")
    socket = SessionSocket(sock)
    await socket.open()
    sink: list[dict[str, Any]] = []
    relay = SessionRelay(
        socket_path=sock, quest_id="cid", title_provider=lambda: "T", events=("all",),
        sink=sink.append, is_subscribed=lambda: True, lease_poll_gap=0.02)
    async with anyio.create_task_group() as tg:
        tg.start_soon(socket.serve)
        tg.start_soon(relay.run)
        await wait_until(lambda: socket.leased_count != 0)
        socket.broadcast(
            {"id": "g1", "type": "awaiting_user_input", "payload": {"prompt": "?"}})
        await wait_until(lambda: socket.leased_count == 0)  # set-lease false crosses the wire
        socket.broadcast(
            {"id": "u1", "type": "user_message", "payload": {"text": "reply"}})
        await wait_until(lambda: socket.leased_count == 1)  # set-lease true crosses the wire
        socket.broadcast(
            {"id": "r1", "type": "agent_message", "payload": {"text": "ok"}})
        await wait_until(lambda: len(sink) >= 3)
        tg.cancel_scope.cancel()
    await socket.close()
    assert [s["type"] for s in sink] == [
        "awaiting_user_input", "user_message", "agent_message"]


@pytest.mark.anyio
async def test_relay_replayed_gate_does_not_touch_lease(tmp_path: Path) -> None:
    # A replayed (marked) gate must not set _awaiting: the lease stays held and the
    # frame still reaches the sink.
    sock = str(tmp_path / "s.sock")
    socket = SessionSocket(sock)
    await socket.open()
    sink: list[dict[str, Any]] = []
    relay = SessionRelay(
        socket_path=sock, quest_id="cid", title_provider=lambda: "T", events=("all",),
        sink=sink.append, is_subscribed=lambda: True, lease_poll_gap=0.02)
    async with anyio.create_task_group() as tg:
        tg.start_soon(socket.serve)
        tg.start_soon(relay.run)
        await wait_until(lambda: socket.leased_count != 0)
        socket.broadcast({"type": "awaiting_user_input",
                          "payload": {"prompt": "old"}, "_replay": True})
        # Event-driven: wait until the replayed frame reaches the sink (proves the
        # relay ingested it, so _awaiting is finalized), then give the poll loop
        # several gaps in which a wrongly-set _awaiting would drop the lease.
        await wait_until(lambda: any(s.get("payload", {}).get("prompt") == "old" for s in sink))
        await anyio.sleep(0.1)             # several poll gaps for any wrong lease-drop
        assert socket.leased_count == 1    # lease untouched
        assert socket.attached_count == 1
        tg.cancel_scope.cancel()
    await socket.close()


@pytest.mark.anyio
async def test_relay_run_raises_when_no_session_is_resident(tmp_path: Path) -> None:
    # The relay attaches to a resident session ONLY; it never spawns one
    # (subscription does not pin residency). No live socket → OSError, not a spawn.
    relay = SessionRelay(
        socket_path=str(tmp_path / "absent.sock"), quest_id="cid", title_provider=lambda: "T",
        events=("all",), sink=lambda _e: None)
    with pytest.raises(OSError):
        await relay.run()


@pytest.mark.anyio
async def test_relay_leases_when_subscribed(tmp_path: Path) -> None:
    sock = str(tmp_path / "s.sock")
    socket = SessionSocket(sock)
    await socket.open()
    relay = SessionRelay(
        socket_path=sock, quest_id="cid", title_provider=lambda: "T", events=("all",),
        sink=lambda _e: None, is_subscribed=lambda: True)
    async with anyio.create_task_group() as tg:
        tg.start_soon(socket.serve)
        tg.start_soon(relay.run)
        await wait_until(lambda: socket.leased_count != 0)
        assert socket.leased_count == 1
        tg.cancel_scope.cancel()
    await socket.close()


@pytest.mark.anyio
async def test_relay_is_lease_less_when_not_subscribed(tmp_path: Path) -> None:
    sock = str(tmp_path / "s.sock")
    socket = SessionSocket(sock)
    await socket.open()
    relay = SessionRelay(
        socket_path=sock, quest_id="cid", title_provider=lambda: "T", events=("all",),
        sink=lambda _e: None, is_subscribed=lambda: False)
    async with anyio.create_task_group() as tg:
        tg.start_soon(socket.serve)
        tg.start_soon(relay.run)
        await wait_until(lambda: socket.attached_count != 0)
        assert socket.leased_count == 0   # attached for observation, but not leased
        tg.cancel_scope.cancel()
    await socket.close()


@pytest.mark.anyio
async def test_relay_set_lease_tracks_subscription_changes(tmp_path: Path) -> None:
    sock = str(tmp_path / "s.sock")
    socket = SessionSocket(sock)
    await socket.open()
    sub = {"on": False}
    relay = SessionRelay(
        socket_path=sock, quest_id="cid", title_provider=lambda: "T", events=("all",),
        sink=lambda _e: None, is_subscribed=lambda: sub["on"], lease_poll_gap=0.02)
    async with anyio.create_task_group() as tg:
        tg.start_soon(socket.serve)
        tg.start_soon(relay.run)
        await wait_until(lambda: socket.attached_count != 0)
        assert socket.leased_count == 0
        sub["on"] = True                       # subscribe-while-running
        await wait_until(lambda: socket.leased_count == 1)
        sub["on"] = False                      # unsubscribe
        await wait_until(lambda: socket.leased_count == 0)
        tg.cancel_scope.cancel()
    await socket.close()


@pytest.mark.anyio
async def test_relay_reflects_title_rename_after_start(tmp_path: Path) -> None:
    # The relay's provider starts empty (session began before the brainstorm named
    # the Quest) and later returns a real title. The FIRST event carries "", and a
    # LATER event carries the rename — the frozen-empty-title bug would keep "".
    sock = str(tmp_path / "s.sock")
    socket = SessionSocket(sock)
    await socket.open()
    holder = {"title": ""}
    sink: list[dict[str, Any]] = []
    relay = SessionRelay(
        socket_path=sock, quest_id="cid",
        title_provider=lambda: holder["title"], events=("all",), sink=sink.append)
    async with anyio.create_task_group() as tg:
        tg.start_soon(socket.serve)
        tg.start_soon(relay.run)
        await wait_until(lambda: socket.attached_count != 0)
        socket.broadcast({"id": "e1", "type": "agent_result", "payload": {}})
        await wait_until(lambda: len(sink) >= 1)
        holder["title"] = "Real Title"     # rename lands mid-session
        socket.broadcast({"id": "e2", "type": "agent_result", "payload": {}})
        await wait_until(lambda: len(sink) >= 2)
        tg.cancel_scope.cancel()
    await socket.close()
    assert sink[0]["quest"]["title"] == ""            # empty at start
    assert sink[1]["quest"]["title"] == "Real Title"   # picked up the rename


@pytest.mark.anyio
async def test_relay_interrupt_record_does_not_retake_lease_at_a_gate(
    tmp_path: Path,
) -> None:
    # An interrupt record is observational: it can arrive after the gate on a
    # mid-run Ctrl-C, and must not clear _awaiting and re-take the lease.
    sock = str(tmp_path / "s.sock")
    socket = SessionSocket(sock)
    await socket.open()
    sink: list[dict[str, Any]] = []
    relay = SessionRelay(
        socket_path=sock, quest_id="cid", title_provider=lambda: "T", events=("all",),
        sink=sink.append, is_subscribed=lambda: True, lease_poll_gap=0.02)
    async with anyio.create_task_group() as tg:
        tg.start_soon(socket.serve)
        tg.start_soon(relay.run)
        await wait_until(lambda: socket.leased_count != 0)
        socket.broadcast({"id": "g1", "type": "awaiting_user_input",
                          "payload": {"prompt": "?"}})
        await wait_until(lambda: socket.leased_count == 0)
        socket.broadcast({"id": "i1", "type": "interrupt_delivered",
                          "payload": {"tier": "cooperative", "outcome": "ok"}})
        # Event-driven: the relay has ingested the record once it reaches the sink.
        await wait_until(lambda: any(s.get("id") == "i1" for s in sink))
        await anyio.sleep(0.1)             # several poll gaps for any wrong re-lease
        assert socket.leased_count == 0    # gate still stands: no lease taken back
        # ...and the poll loop is alive — a real run event DOES take the lease back.
        socket.broadcast({"id": "m1", "type": "agent_message", "payload": {}})
        await wait_until(lambda: socket.leased_count == 1)
        tg.cancel_scope.cancel()
    await socket.close()
