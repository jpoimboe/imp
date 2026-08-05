"""AC 8: a leased session outlives the first-attach grace. AC 6: interrupt pauses the run."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import anyio
import pytest

from imp.api import QuestHandle, attach, create_quest
from imp.cli.signals import InterruptCoordinator
from imp.engine.awaiting_user_input import AwaitingUserInput
from imp.remote.session.discover import session_socket_path
from imp.remote.session.server import SessionServer
from tests.async_wait import wait_until
from tests.conftest import _seed_control

pytestmark = pytest.mark.timeout(240)

# The daemon's own grace is 30s (server.py:_DEFAULT_FIRST_ATTACH_TIMEOUT). An
# in-process server lets the test set it, so "past the grace" is a real multiple of
# it rather than a 40s wall-clock wait; the unleased case below is the falsifier.
# No test races this clock: run_fn waits for `ready`, which each test sets only
# after the daemon has registered its attach, so the grace can never start first.
# The barrier polls attached_count, not leased_count: the daemon builds the client
# with its lease bit and registers it in one step (socket_server.py:269-274), so the
# two counters flip together and the leased case can assert the lease outright.
_GRACE = 0.5


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _draft(root: Path) -> QuestHandle:
    _seed_control(root)
    return create_quest(root, "interview me")


def _gate_run_fn(outcome: dict[str, Any], ready: anyio.Event) -> Any:
    """Opens one gate and waits; hibernation surfaces as AwaitingUserInput."""

    # Held at `ready` so the gate — and with it the grace clock — opens only once the
    # test's attach is registered. The wait is a barrier, never a tuned window.
    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        await ready.wait()
        on_event({"type": "awaiting_user_input", "payload": {"prompt": "?"}})
        try:
            outcome["answer"] = await user_chat_input_fn("?")
        except AwaitingUserInput:
            outcome["hibernated"] = True
        return "done"

    return run_fn


async def _await_live_gate(session: Any) -> None:
    async for event in session.events():
        if event.type == "awaiting_user_input" and not event.raw.get("_replay"):
            return
    raise AssertionError("stream ended before any live gate opened")


@pytest.mark.anyio
async def test_a_leased_session_holds_an_open_gate_past_the_first_attach_grace(
    tmp_path: Path,
) -> None:
    handle = _draft(tmp_path)
    outcome: dict[str, Any] = {}
    ready = anyio.Event()
    server = SessionServer(
        str(session_socket_path(handle.id)),
        hibernate=True, first_attach_timeout=_GRACE)

    async with anyio.create_task_group() as tg:
        await server.open()
        tg.start_soon(server.serve, _gate_run_fn(outcome, ready))

        async with attach(tmp_path, handle) as session:      # leased by default
            # Open the gate only once the lease is registered: the daemon then never
            # enters _await_first_attach at all, so no wall clock decides this test.
            await wait_until(lambda: server.attached_count == 1)
            assert server.leased_count == 1
            ready.set()
            await _await_live_gate(session)
            await anyio.sleep(_GRACE * 6)                    # well past the grace
            assert "hibernated" not in outcome
            await session.send_input("us")
            await wait_until(lambda: "answer" in outcome)

        assert outcome["answer"] == "us"
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_an_unleased_session_lets_the_daemon_hibernate_at_the_grace(
    tmp_path: Path,
) -> None:
    handle = _draft(tmp_path)
    outcome: dict[str, Any] = {}
    ready = anyio.Event()
    server = SessionServer(
        str(session_socket_path(handle.id)),
        hibernate=True, first_attach_timeout=_GRACE)

    async with anyio.create_task_group() as tg:
        await server.open()
        tg.start_soon(server.serve, _gate_run_fn(outcome, ready))

        async with attach(tmp_path, handle, lease=False) as session:
            await wait_until(lambda: server.attached_count == 1)
            ready.set()
            await _await_live_gate(session)
            # _GRACE is a floor on how long this takes, never a deadline: an
            # unleased client can only ever delay the hibernation being polled for.
            await wait_until(lambda: "hibernated" in outcome)

        assert "answer" not in outcome
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_interrupt_pauses_the_run_the_way_ctrl_c_does(tmp_path: Path) -> None:
    handle = _draft(tmp_path)
    coord = InterruptCoordinator()
    server = SessionServer(
        str(session_socket_path(handle.id)), coordinator=coord, hibernate=False)

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        on_event({"type": "step_started", "payload": {"step": "plan"}})
        await anyio.sleep_forever()
        return "unreachable"

    async with anyio.create_task_group() as tg:
        await server.open()
        tg.start_soon(server.serve, run_fn)

        async with attach(tmp_path, handle) as session:
            await wait_until(lambda: server.attached_count == 1)
            await session.interrupt("stop")
            await wait_until(lambda: coord.interrupted)

        # Identical to what the terminal driver's Ctrl-C produces.
        assert coord.pause_reason == "ctrl-c"
        tg.cancel_scope.cancel()
