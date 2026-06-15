from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import anyio
import pytest

from imp.cli.session_terminal import run_terminal_client
from imp.cli.signals import InterruptCoordinator
from imp.remote.session.client import SessionClient
from imp.remote.session.server import SessionServer
from imp.remote.session.socket_server import SessionSocket


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_terminal_client_renders_events_and_answers_one_gate(
    tmp_path: Path,
) -> None:
    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=False)
    received: list[str] = []

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        with anyio.fail_after(5):
            while server.attached_count == 0:
                await anyio.sleep(0.02)
        on_event({"type": "step_started", "payload": {}})
        on_event({"type": "awaiting_user_input",
                   "payload": {"prompt": "your name?"}})
        reply = await user_chat_input_fn("your name?")
        received.append(reply)
        on_event({"type": "user_message", "payload": {"text": reply}})
        return reply

    rendered: list[str] = []
    prompts: list[str] = []

    class _Driver:
        def on_event(self, event: dict[str, Any]) -> None:
            rendered.append(event.get("type"))

        def finish(self) -> None:
            rendered.append("__finish__")

    async def _attach(
        on_event: Callable[[dict[str, Any]], None],
    ) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    async def _solicit(prompt: str) -> str:
        prompts.append(prompt)
        return "imp"

    coord = InterruptCoordinator()
    with anyio.fail_after(10):
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=coord, solicit=_solicit)

            tg.start_soon(_client)

    assert received == ["imp"]
    assert prompts == ["your name?"]
    assert rendered == [
        "step_started", "awaiting_user_input", "user_message", "__finish__"]


@pytest.mark.anyio
async def test_terminal_client_forwards_interrupt_gesture_as_a_control_frame(
    tmp_path: Path,
) -> None:
    sock = str(tmp_path / "s.sock")
    actions: list[str] = []
    server = SessionSocket(sock, on_control=actions.append)
    coord = InterruptCoordinator()

    async def _attach(
        on_event: Callable[[dict[str, Any]], None],
    ) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    async def _solicit(_prompt: str) -> str:
        raise AssertionError("no gate fired; solicit must not be called")

    class _Driver:
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...

    client_returned = anyio.Event()
    with anyio.fail_after(10):
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=coord, solicit=_solicit)
                client_returned.set()

            tg.start_soon(_client)
            # Fire the gesture once the forwarder is attached; before attachment
            # signal_pause has no runner and sends nothing, so retry until the
            # interrupt frame lands on the socket.
            with anyio.fail_after(5):
                while not actions:
                    await coord.signal_pause()
                    await anyio.sleep(0.02)
            assert actions == ["interrupt"]
            assert server.attached_count == 1   # gesture did not detach the client
            await server.close()                # daemon ends → socket drops
            with anyio.fail_after(5):
                await client_returned.wait()
            tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_terminal_client_detaches_interrupt_forwarder_on_return(
    tmp_path: Path,
) -> None:
    sock = str(tmp_path / "s.sock")
    server = SessionSocket(sock)
    coord = InterruptCoordinator()

    async def _attach(
        on_event: Callable[[dict[str, Any]], None],
    ) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    async def _solicit(_prompt: str) -> str:
        raise AssertionError("no gate fired; solicit must not be called")

    class _Driver:
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...

    returned = anyio.Event()
    with anyio.fail_after(10):
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=coord, solicit=_solicit)
                returned.set()

            tg.start_soon(_client)
            with anyio.fail_after(5):
                while server.attached_count != 1:
                    await anyio.sleep(0.02)
            await server.close()              # daemon ends → client returns
            with anyio.fail_after(5):
                await returned.wait()
            # The forwarder must be detached: with it still attached this would
            # call send_control on the now-closed client and raise RuntimeError.
            await coord.signal_pause()
            tg.cancel_scope.cancel()
