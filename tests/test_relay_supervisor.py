from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import anyio
import pytest

from imp.remote.daemon.relay_supervisor import RelaySupervisor
from imp.remote.session.discover import session_socket_path
from imp.remote.session.socket_server import SessionSocket


def _make_home(control_root: Path, quest_id: str) -> Path:
    home = control_root / ".imp" / "quests" / quest_id
    home.mkdir(parents=True, exist_ok=True)
    (home / "quest.json").write_text("{}")
    return home


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
async def _ignore_anyio_teardown_setresult_race() -> AsyncIterator[None]:
    """Swallow anyio 4.8's benign socket-teardown InvalidStateError.

    anyio's asyncio backend registers the bound ``future.set_result`` as the raw
    fd-readable reader for UNIXSocketListener.accept and stream reads
    (_backends/_asyncio.py:1331,1550), and only removes the reader in the future's
    done-callback. When a cancel scope tears the socket down in the SAME loop turn
    the fd goes readable, the already-queued ``set_result`` fires on the just-
    cancelled future -> InvalidStateError. It is raised inside a Handle, so asyncio
    routes it to the loop exception handler, which pytest-anyio promotes to a test
    failure -- making the open/close/reopen tests flaky (~1/13). Delegate every
    other loop exception to the previous handler untouched."""
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()

    def handler(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        exc = context.get("exception")
        callback = getattr(context.get("handle"), "_callback", None)
        future = getattr(callback, "__self__", None)
        if (
            isinstance(exc, asyncio.InvalidStateError)
            and getattr(callback, "__name__", "") == "set_result"
            and isinstance(future, asyncio.Future)
            and future.cancelled()
        ):
            return  # the anyio accept/read set_result-on-cancelled-future race
        if previous is not None:
            previous(loop, context)
        else:
            loop.default_exception_handler(context)

    loop.set_exception_handler(handler)
    try:
        yield
    finally:
        loop.set_exception_handler(previous)


@pytest.mark.anyio
async def test_supervisor_relays_a_resident_sessions_events(
    tmp_path: Path,
) -> None:
    # One resident session daemon; the supervisor attaches a relay and forwards its
    # curated events to the sink with the namespaced change context.
    cid = "live-one"
    _make_home(tmp_path, cid)
    sock = SessionSocket(str(session_socket_path(tmp_path, cid)))
    await sock.open()
    sink: list[dict[str, Any]] = []
    sup = RelaySupervisor(
        control_root=tmp_path, sink=sink.append, events=("all",),
        title_for=lambda _cid: "T", poll_gap=0.02)
    async with anyio.create_task_group() as tg:
        tg.start_soon(sock.serve)
        tg.start_soon(sup.run)
        with anyio.fail_after(5):
            while sock.attached_count == 0:
                await anyio.sleep(0.02)
        sock.broadcast(
            {"id": "e1", "type": "agent_output", "payload": {"text": "hi"}})
        with anyio.fail_after(5):
            while not sink:
                await anyio.sleep(0.02)
        tg.cancel_scope.cancel()
    await sock.close()
    assert sink[0]["id"] == "e1"
    assert sink[0]["type"] == "agent_output"
    assert sink[0]["change"] == {
        "id": cid, "title": "T", "step": None, "status": "running"}


@pytest.mark.anyio
async def test_supervisor_multiplexes_two_resident_sessions(
    tmp_path: Path,
) -> None:
    # Two resident session daemons; the supervisor runs a relay per change, so both
    # changes' events reach the one sink, each carrying its OWN per-change title.
    _make_home(tmp_path, "a")
    _make_home(tmp_path, "b")
    sock_a = SessionSocket(str(session_socket_path(tmp_path, "a")))
    sock_b = SessionSocket(str(session_socket_path(tmp_path, "b")))
    await sock_a.open()
    await sock_b.open()
    sink: list[dict[str, Any]] = []
    sup = RelaySupervisor(
        control_root=tmp_path, sink=sink.append, events=("all",),
        title_for=lambda cid: cid.upper(), poll_gap=0.02)
    async with anyio.create_task_group() as tg:
        tg.start_soon(sock_a.serve)
        tg.start_soon(sock_b.serve)
        tg.start_soon(sup.run)
        with anyio.fail_after(5):
            while sock_a.attached_count == 0 or sock_b.attached_count == 0:
                await anyio.sleep(0.02)
        sock_a.broadcast({"id": "ea", "type": "agent_output", "payload": {}})
        sock_b.broadcast({"id": "eb", "type": "agent_output", "payload": {}})
        with anyio.fail_after(5):
            while len(sink) < 2:
                await anyio.sleep(0.02)
        tg.cancel_scope.cancel()
    await sock_a.close()
    await sock_b.close()
    titles = {e["id"]: e["change"]["title"] for e in sink}
    assert titles == {"ea": "A", "eb": "B"}


@pytest.mark.anyio
async def test_supervisor_reattaches_after_relay_exits(
    tmp_path: Path,
) -> None:
    # When a relay exits naturally (socket closed), the supervisor must clear the
    # running set and re-attach on the next poll — falsifies the discard() call.
    cid = "exiting-one"
    _make_home(tmp_path, cid)
    sock = SessionSocket(str(session_socket_path(tmp_path, cid)))
    await sock.open()
    sink: list[dict[str, Any]] = []
    sup = RelaySupervisor(
        control_root=tmp_path, sink=sink.append, events=("all",),
        title_for=lambda _cid: "T", poll_gap=0.02)
    async with anyio.create_task_group() as tg:
        tg.start_soon(sock.serve)
        tg.start_soon(sup.run)
        # Wait for first attach.
        with anyio.fail_after(5):
            while sock.attached_count == 0:
                await anyio.sleep(0.02)
        # Close the socket so the relay exits naturally.
        await sock.close()
        with anyio.fail_after(5):
            while sock.attached_count != 0:
                await anyio.sleep(0.02)
        # Re-open; the supervisor must re-attach.
        await sock.open()
        tg.start_soon(sock.serve)
        with anyio.fail_after(5):
            while sock.attached_count == 0:
                await anyio.sleep(0.02)
        tg.cancel_scope.cancel()
    await sock.close()


@pytest.mark.anyio
async def test_supervisor_keeps_relaying_across_a_gate(tmp_path: Path) -> None:
    # No latch: after a gate event, a subsequent live event on a still-resident
    # session still reaches the sink because the relay never detached.
    cid = "gated-one"
    _make_home(tmp_path, cid)
    sock = SessionSocket(str(session_socket_path(tmp_path, cid)))
    await sock.open()
    sink: list[dict[str, Any]] = []
    sup = RelaySupervisor(
        control_root=tmp_path, sink=sink.append, events=("all",),
        title_for=lambda _cid: "T", poll_gap=0.02)
    async with anyio.create_task_group() as tg:
        tg.start_soon(sock.serve)
        tg.start_soon(sup.run)
        with anyio.fail_after(5):
            while sock.attached_count == 0:
                await anyio.sleep(0.02)
        sock.broadcast(
            {"id": "g1", "type": "awaiting_user_input", "payload": {"prompt": "?"}})
        sock.broadcast(
            {"id": "m1", "type": "user_message", "payload": {"text": "reply"}})
        with anyio.fail_after(5):
            while len(sink) < 2:
                await anyio.sleep(0.02)
        assert sock.attached_count == 1   # never detached across the gate
        tg.cancel_scope.cancel()
    await sock.close()
    assert [s["type"] for s in sink] == ["awaiting_user_input", "user_message"]


@pytest.mark.anyio
async def test_supervisor_subscribed_session_drops_lease_at_gate(
    tmp_path: Path,
) -> None:
    from imp.remote.daemon.subscriptions import Subscriptions

    # A subscribed-only session still hibernates at the gate: the relay drops its
    # lease (leased_count -> 0) so the server's unleased path would fire, while the
    # relay stays attached for the session's residency.
    cid = "subbed-gate"
    _make_home(tmp_path, cid)
    sock = SessionSocket(str(session_socket_path(tmp_path, cid)))
    await sock.open()
    subs = Subscriptions()
    subs.add(cid)
    sup = RelaySupervisor(
        control_root=tmp_path, sink=lambda _e: None, events=("all",),
        title_for=lambda _c: "T", poll_gap=0.02, subscriptions=subs)
    async with anyio.create_task_group() as tg:
        tg.start_soon(sock.serve)
        tg.start_soon(sup.run)
        with anyio.fail_after(5):
            while sock.leased_count == 0:
                await anyio.sleep(0.02)
        sock.broadcast(
            {"type": "awaiting_user_input", "payload": {"prompt": "?"}})
        with anyio.fail_after(5):
            while sock.leased_count != 0:
                await anyio.sleep(0.02)
        assert sock.attached_count == 1   # dropped its lease, kept its attachment
        tg.cancel_scope.cancel()
    await sock.close()


@pytest.mark.anyio
async def test_supervisor_leases_only_subscribed_changes(
    tmp_path: Path,
) -> None:
    from imp.remote.daemon.subscriptions import Subscriptions

    cid = "subbed"
    _make_home(tmp_path, cid)
    sock = SessionSocket(str(session_socket_path(tmp_path, cid)))
    await sock.open()
    subs = Subscriptions()
    subs.add(cid)
    sup = RelaySupervisor(
        control_root=tmp_path, sink=lambda _e: None, events=("all",),
        title_for=lambda _c: "T", poll_gap=0.02, subscriptions=subs)
    async with anyio.create_task_group() as tg:
        tg.start_soon(sock.serve)
        tg.start_soon(sup.run)
        with anyio.fail_after(5):
            while sock.leased_count == 0:
                await anyio.sleep(0.02)
        assert sock.leased_count == 1
        tg.cancel_scope.cancel()
    await sock.close()


@pytest.mark.anyio
async def test_supervisor_reflects_late_title_via_live_provider(
    tmp_path: Path,
) -> None:
    # The supervisor's title_for returns "" at first (session began before the
    # rename) and a real title later. The relay's live provider must reflect the
    # rename on a later event — the frozen-empty-title bug would keep "".
    cid = "renamed-mid"
    _make_home(tmp_path, cid)
    sock = SessionSocket(str(session_socket_path(tmp_path, cid)))
    await sock.open()
    holder = {"title": ""}
    sink: list[dict[str, Any]] = []
    sup = RelaySupervisor(
        control_root=tmp_path, sink=sink.append, events=("all",),
        title_for=lambda _cid: holder["title"], poll_gap=0.02)
    async with anyio.create_task_group() as tg:
        tg.start_soon(sock.serve)
        tg.start_soon(sup.run)
        with anyio.fail_after(5):
            while sock.attached_count == 0:
                await anyio.sleep(0.02)
        sock.broadcast({"id": "e1", "type": "agent_output", "payload": {}})
        with anyio.fail_after(5):
            while len(sink) < 1:
                await anyio.sleep(0.02)
        holder["title"] = "Real Title"
        sock.broadcast({"id": "e2", "type": "agent_output", "payload": {}})
        with anyio.fail_after(5):
            while len(sink) < 2:
                await anyio.sleep(0.02)
        tg.cancel_scope.cancel()
    await sock.close()
    assert sink[0]["change"]["title"] == ""
    assert sink[1]["change"]["title"] == "Real Title"
