from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import anyio
import pytest

from imp.remote.daemon.discover import ChangeInfo
from imp.remote.daemon.relay_supervisor import RelaySupervisor
from imp.remote.session.discover import session_socket_path
from imp.remote.session.socket_server import SessionSocket


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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # One resident session daemon; the supervisor attaches a relay and forwards its
    # curated events to the sink with the namespaced change context.
    cid = "live-one"
    monkeypatch.setattr(
        "imp.remote.daemon.directory.list_changes",
        lambda _cr: [ChangeInfo(id=cid, title="T", status="running", step="plan")])
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Two resident session daemons; the supervisor runs a relay per change, so both
    # changes' events reach the one sink, each carrying its OWN per-change title.
    infos = [
        ChangeInfo(id="a", title="A", status="running", step="plan"),
        ChangeInfo(id="b", title="B", status="running", step="plan"),
    ]
    monkeypatch.setattr(
        "imp.remote.daemon.directory.list_changes", lambda _cr: infos)
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # When a relay exits naturally (socket closed), the supervisor must clear the
    # running set and re-attach on the next poll — falsifies the discard() call.
    cid = "exiting-one"
    monkeypatch.setattr(
        "imp.remote.daemon.directory.list_changes",
        lambda _cr: [ChangeInfo(id=cid, title="T", status="running", step="plan")])
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
async def test_supervisor_does_not_reattach_across_a_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The relay detaches at the awaiting_user_input gate so the session can
    # hibernate (subscription does not pin residency). Even while the socket stays
    # live, the supervisor must NOT re-attach across that gate — re-attaching would
    # pin the change resident and it could never hibernate.
    cid = "gated-one"
    monkeypatch.setattr(
        "imp.remote.daemon.directory.list_changes",
        lambda _cr: [ChangeInfo(id=cid, title="T", status="running", step="plan")])
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
            {"type": "awaiting_user_input", "payload": {"prompt": "go?"}})
        # Relay forwards the gate event then detaches → attached_count drops to 0.
        with anyio.fail_after(5):
            while sock.attached_count != 0:
                await anyio.sleep(0.02)
        # Several poll cycles (0.2 s ≫ 0.02 s poll); the supervisor must NOT re-attach.
        await anyio.sleep(0.2)
        assert sock.attached_count == 0
        tg.cancel_scope.cancel()
    await sock.close()
    assert sink[-1]["type"] == "awaiting_user_input"


@pytest.mark.anyio
async def test_supervisor_reattaches_after_rehydration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # After a gate detach the change hibernates (drops out of the directory); a later
    # reply rehydrates it (reappears). Gate avoidance suppresses re-attach only WHILE
    # the change stays continuously resident — once it goes non-resident the latch
    # clears, so the supervisor re-attaches on rehydration.
    cid = "rehydrated-one"
    visible = {"resident": True}

    def _list(_cr: Any) -> list[ChangeInfo]:
        if visible["resident"]:
            return [ChangeInfo(id=cid, title="T", status="running", step="plan")]
        return []

    monkeypatch.setattr("imp.remote.daemon.directory.list_changes", _list)
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
            {"type": "awaiting_user_input", "payload": {"prompt": "go?"}})
        with anyio.fail_after(5):
            while sock.attached_count != 0:
                await anyio.sleep(0.02)
        # Hibernate: the change leaves the directory, clearing the gate latch.
        visible["resident"] = False
        await anyio.sleep(0.1)
        # Rehydrate: it reappears → the supervisor re-attaches.
        visible["resident"] = True
        with anyio.fail_after(5):
            while sock.attached_count == 0:
                await anyio.sleep(0.02)
        tg.cancel_scope.cancel()
    await sock.close()


@pytest.mark.anyio
async def test_supervisor_leases_only_subscribed_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from imp.remote.daemon.subscriptions import Subscriptions

    cid = "subbed"
    monkeypatch.setattr(
        "imp.remote.daemon.directory.list_changes",
        lambda _cr: [ChangeInfo(id=cid, title="T", status="running", step="plan")])
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
