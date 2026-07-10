from __future__ import annotations

from pathlib import Path
from typing import Any

import anyio
import pytest

from imp.remote.session.relay import SessionRelay
from imp.remote.session.socket_server import SessionSocket


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
        with anyio.fail_after(5):
            while socket.attached_count == 0:
                await anyio.sleep(0.02)
        socket.broadcast(
            {"id": "e1", "type": "agent_output", "payload": {"text": "hi"}})
        with anyio.fail_after(5):
            while not sink:
                await anyio.sleep(0.02)
        tg.cancel_scope.cancel()
    await socket.close()
    assert sink[0]["id"] == "e1"
    assert sink[0]["type"] == "agent_output"
    assert sink[0]["change"] == {
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
        with anyio.fail_after(5):
            while socket.leased_count == 0:
                await anyio.sleep(0.02)
        assert socket.attached_count == 1
        socket.broadcast(
            {"type": "awaiting_user_input", "payload": {"prompt": "go?"}})
        # Gate drops the lease within one poll gap.
        with anyio.fail_after(5):
            while socket.leased_count != 0:
                await anyio.sleep(0.02)
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
        with anyio.fail_after(5):
            while socket.leased_count == 0:
                await anyio.sleep(0.02)
        socket.broadcast(
            {"id": "g1", "type": "awaiting_user_input", "payload": {"prompt": "?"}})
        with anyio.fail_after(5):          # set-lease false crosses the wire
            while socket.leased_count != 0:
                await anyio.sleep(0.02)
        socket.broadcast(
            {"id": "u1", "type": "user_message", "payload": {"text": "reply"}})
        with anyio.fail_after(5):          # set-lease true crosses the wire
            while socket.leased_count != 1:
                await anyio.sleep(0.02)
        socket.broadcast(
            {"id": "r1", "type": "llm_response", "payload": {"text": "ok"}})
        with anyio.fail_after(5):
            while len(sink) < 3:
                await anyio.sleep(0.02)
        tg.cancel_scope.cancel()
    await socket.close()
    assert [s["type"] for s in sink] == [
        "awaiting_user_input", "user_message", "llm_response"]


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
        with anyio.fail_after(5):
            while socket.leased_count == 0:
                await anyio.sleep(0.02)
        socket.broadcast({"type": "awaiting_user_input",
                          "payload": {"prompt": "old"}, "_replay": True})
        # Event-driven: wait until the replayed frame reaches the sink (proves the
        # relay ingested it, so _awaiting is finalized), then give the poll loop
        # several gaps in which a wrongly-set _awaiting would drop the lease.
        with anyio.fail_after(5):
            while not any(
                    s.get("payload", {}).get("prompt") == "old" for s in sink):
                await anyio.sleep(0.02)
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
        with anyio.fail_after(5):
            while socket.leased_count == 0:
                await anyio.sleep(0.02)
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
        with anyio.fail_after(5):
            while socket.attached_count == 0:
                await anyio.sleep(0.02)
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
        with anyio.fail_after(5):
            while socket.attached_count == 0:
                await anyio.sleep(0.02)
        assert socket.leased_count == 0
        sub["on"] = True                       # subscribe-while-running
        with anyio.fail_after(5):
            while socket.leased_count != 1:
                await anyio.sleep(0.02)
        sub["on"] = False                      # unsubscribe
        with anyio.fail_after(5):
            while socket.leased_count != 0:
                await anyio.sleep(0.02)
        tg.cancel_scope.cancel()
    await socket.close()


@pytest.mark.anyio
async def test_relay_reflects_title_rename_after_start(tmp_path: Path) -> None:
    # The relay's provider starts empty (session began before the brainstorm named
    # the Change) and later returns a real title. The FIRST event carries "", and a
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
        with anyio.fail_after(5):
            while socket.attached_count == 0:
                await anyio.sleep(0.02)
        socket.broadcast({"id": "e1", "type": "agent_output", "payload": {}})
        with anyio.fail_after(5):
            while len(sink) < 1:
                await anyio.sleep(0.02)
        holder["title"] = "Real Title"     # rename lands mid-session
        socket.broadcast({"id": "e2", "type": "agent_output", "payload": {}})
        with anyio.fail_after(5):
            while len(sink) < 2:
                await anyio.sleep(0.02)
        tg.cancel_scope.cancel()
    await socket.close()
    assert sink[0]["change"]["title"] == ""            # empty at start
    assert sink[1]["change"]["title"] == "Real Title"   # picked up the rename
