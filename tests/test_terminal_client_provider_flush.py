from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import anyio
import pytest

from imp.cli.session_terminal import run_terminal_client
from imp.cli.signals import InterruptCoordinator
from imp.remote.session.client import SessionClient
from imp.remote.session.socket_server import SessionSocket
from tests.async_wait import wait_until


async def _no_confirm(question: str) -> bool:
    raise AssertionError(f"unexpected confirm: {question!r}")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _Driver:
    def on_event(self, _event: dict[str, Any]) -> None: ...
    def finish(self, **_kw: object) -> None: ...


async def _solicit(_prompt: str, _interrupt: bool = False) -> str:
    raise AssertionError("no gate fired; solicit must not be called")


async def _drive(sock: str, provider: str | None) -> list[tuple[str, str | None]]:
    frames: list[tuple[str, str | None]] = []
    server = SessionSocket(sock, on_control=lambda a, r: frames.append((a, r)))
    coord = InterruptCoordinator()

    async def _attach(on_event: Callable[[dict[str, Any]], None]) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    with anyio.fail_after(30):      # deadlock backstop only
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(), coord=coord,
                    solicit=_solicit, confirm=_no_confirm, leave_mode="new", provider=provider)

            tg.start_soon(_client)
            await wait_until(lambda: server.attached_count == 1)
            if provider is not None:
                await wait_until(
                    lambda: any(f[0] == "set-provider" for f in frames))
            await server.close()
            tg.cancel_scope.cancel()
    return frames


@pytest.mark.anyio
async def test_a_pending_provider_is_sent_once_the_client_is_bound(
    tmp_path: Path,
) -> None:
    frames = await _drive(str(tmp_path / "s.sock"), "codex")
    assert [f for f in frames if f[0] == "set-provider"] == [
        ("set-provider", "codex")]


@pytest.mark.anyio
async def test_no_provider_sends_no_frame(tmp_path: Path) -> None:
    frames = await _drive(str(tmp_path / "s.sock"), None)
    assert [f for f in frames if f[0] == "set-provider"] == []
