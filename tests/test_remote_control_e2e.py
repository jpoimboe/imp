from __future__ import annotations

import functools
from collections.abc import Callable
from pathlib import Path

import anyio
import pytest

from imp.remote.daemon.commands import HELP_TEXT, ROUTE_HINT
from imp.remote.daemon.discover import ChangeInfo, socket_is_live, socket_path
from imp.remote.daemon.runner import run_daemon
from imp.remote.session.discover import session_socket_path
from imp.remote.session.socket_server import SessionSocket
from tests.conftest import _seed_control
from tests.playbooks.fake_telegram import FakeTelegram

CHANGE = "2026-06-13-zzzzzz"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _wait(predicate: Callable[[], bool], timeout: float = 10.0) -> None:
    with anyio.fail_after(timeout):
        while not predicate():
            await anyio.sleep(0.05)


def _text_in_outbox(fake: FakeTelegram, needle: str) -> bool:
    return any(needle in m["text"] for m in fake.outbox())


@pytest.mark.anyio
async def test_e2e_chat_commands_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    monkeypatch.setattr(
        "imp.remote.daemon.directory.list_changes",
        lambda _cr: [ChangeInfo(id=CHANGE, title="Dark mode",
                                status="running", step="plan")])
    monkeypatch.setattr(
        "imp.remote.daemon.discover.snapshot",
        lambda _cr, c: ("running", "plan", "Dark mode") if c == CHANGE else None)
    fake = FakeTelegram(chat_id=4242)
    fake.start()
    monkeypatch.setenv("IMP_TELEGRAM_API_BASE", fake.url)

    created: list[str] = []
    abandoned: list[str] = []
    listed = [ChangeInfo(id="2026-06-13-q1w2e3", title="Listed",
                         status="running", step="plan")]
    spath = socket_path(tmp_path)
    shutdown = anyio.Event()

    delivered: list[tuple[str, str]] = []

    async def _capture_deliver(cid: str, text: str) -> None:
        delivered.append((cid, text))

    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(functools.partial(
                run_daemon, tmp_path,
                env={"IMP_TELEGRAM_TOKEN": "TOK", "IMP_TELEGRAM_CHAT_ID": "4242"},
                create=created.append,
                abandon=lambda cid: (abandoned.append(cid), 0)[1],
                list_changes=lambda: listed,
                deliver=_capture_deliver,
                install_signals=False, shutdown=shutdown))

            with anyio.fail_after(5):
                while not socket_is_live(spath):
                    await anyio.sleep(0.02)

            # A resident session daemon serves its socket; the supervisor attaches
            # a relay and pulls its events, so the adapter records the handle/msg map.
            sess = SessionSocket(str(session_socket_path(tmp_path, CHANGE)))
            await sess.open()
            tg.start_soon(sess.serve)
            await _wait(lambda: sess.attached_count > 0)
            await sess.broadcast(
                {"type": "awaiting_user_input", "payload": {"prompt": "go?"}})
            await _wait(lambda: _text_in_outbox(fake, "Dark mode"))
            m1 = next(m["message_id"] for m in fake.outbox() if "Dark mode" in m["text"])

            fake.inject("/help")
            await _wait(lambda: _text_in_outbox(fake, HELP_TEXT))

            fake.inject("/new add a foo() helper")
            await _wait(lambda: created == ["add a foo() helper"])
            await _wait(lambda: _text_in_outbox(fake, "Spawning"))

            fake.inject("/list")
            await _wait(lambda: _text_in_outbox(fake, "@q1w2e3  Listed — running · plan"))

            # A threaded reply is delivered into the change's session (attach + inject).
            fake.inject_reply("approved", reply_to_message_id=m1)
            await _wait(lambda: delivered == [(CHANGE, "approved")])

            # /abandon, addressed by replying to the Change's message.
            fake.inject_reply("/abandon", reply_to_message_id=m1)
            await _wait(lambda: abandoned == [CHANGE])
            await _wait(lambda: _text_in_outbox(fake, "Abandoned @zzzzzz."))

            # An unroutable bare message gets a hint, never a silent drop.
            fake.inject("who is this even")
            await _wait(lambda: _text_in_outbox(fake, ROUTE_HINT))

            await sess.close()
            shutdown.set()
    finally:
        fake.stop()
