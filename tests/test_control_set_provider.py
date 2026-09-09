from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anyio
import pytest

from imp.engine.quest import Quest
from imp.remote.session.client import SessionClient
from imp.remote.session.daemon import serve_quest_session
from imp.remote.session.discover import session_socket_path
from imp.remote.session.protocol import (
    CONTROL_SET_PROVIDER,
    encode_control,
    parse_client_message,
)
from imp.remote.session.server import SessionServer
from tests.async_wait import wait_until
from tests.conftest import _seed_control


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _stub_stack_dumper(monkeypatch: pytest.MonkeyPatch) -> None:
    import imp.remote.session.daemon as daemon
    monkeypatch.setattr(daemon, "install_stack_dumper", lambda p: None)


def test_the_frame_carries_the_provider_name_over_the_wire() -> None:
    parsed = parse_client_message(encode_control(CONTROL_SET_PROVIDER, "codex"))
    assert (parsed.action, parsed.reason) == ("set-provider", "codex")


@pytest.mark.anyio
async def test_the_handler_writes_the_pin_without_pausing(tmp_path: Path) -> None:
    from imp.cli.signals import InterruptCoordinator

    written: list[str] = []
    coord = InterruptCoordinator()
    server = SessionServer(str(tmp_path / "s.sock"), coordinator=coord,
                           set_provider=written.append)

    await server._dispatch_control(CONTROL_SET_PROVIDER, "codex")

    assert written == ["codex"]
    assert coord.should_pause is False


@pytest.mark.anyio
async def test_the_handler_re_opens_a_parked_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A non-parking frame typed at a solicit would otherwise leave the client
    # blocking on gate_recv for a prompt frame that never arrives.
    from imp.cli.signals import InterruptCoordinator

    server = SessionServer(str(tmp_path / "s.sock"),
                           coordinator=InterruptCoordinator(),
                           set_provider=lambda _n: None)
    prompt = {"type": "awaiting_user_input", "payload": {"steer_hint": "?"}}
    server.set_pending_prompt(prompt)
    broadcasts: list[dict[str, Any]] = []
    monkeypatch.setattr(server._socket, "broadcast", broadcasts.append)

    await server._dispatch_control(CONTROL_SET_PROVIDER, "codex")

    assert broadcasts == [prompt]


@pytest.mark.anyio
async def test_the_daemon_callback_writes_disk_and_the_live_quest(
    tmp_path: Path,
) -> None:
    _seed_control(tmp_path)
    quest = Quest.create(tmp_path, title="demo", quest_id="feat",
                         base_branch="main")
    sock = str(session_socket_path(quest.id))
    running = anyio.Event()

    async def fake_run(quest: Quest, *, on_event: Any, user_chat_input_fn: Any,
                       coordinator: Any, wait_for_turn_fn: Any = None) -> str:
        running.set()
        await anyio.sleep_forever()
        return "x"

    with anyio.fail_after(30):     # deadlock backstop only
        async with anyio.create_task_group() as tg:
            async def _serve() -> None:
                await serve_quest_session(
                    quest, run_fn=fake_run, hibernate=False)

            tg.start_soon(_serve)
            await wait_until(lambda: Path(sock).exists())
            client = SessionClient(sock, quest.id, quest.title,
                                   on_event=lambda _e: None)
            await client.connect()
            await running.wait()

            await client.send_control(CONTROL_SET_PROVIDER, "codex")

            # Durable at the instant of the gesture, and visible in-process on
            # the very Quest run_fn holds — no reload.
            await wait_until(lambda: quest.provider == "codex")
            assert json.loads(
                (quest.quest_dir / "quest.json").read_text())["provider"] == "codex"
            tg.cancel_scope.cancel()
