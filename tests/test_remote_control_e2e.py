from __future__ import annotations

import functools
import json
from collections.abc import Callable
from pathlib import Path

import anyio
import pytest
from anyio.streams.buffered import BufferedByteReceiveStream

from imp.remote.daemon.commands import HELP_TEXT, ROUTE_HINT
from imp.remote.daemon.discover import ChangeInfo, socket_is_live, socket_path
from imp.remote.daemon.protocol import make_hello
from imp.remote.daemon.runner import run_daemon
from tests.conftest import _seed_control
from tests.playbooks.fake_telegram import FakeTelegram

CHANGE = "2026-06-13-zzzzzz"
_MAX = 1 << 20


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
    fake = FakeTelegram(chat_id=4242)
    fake.start()
    monkeypatch.setenv("IMP_TELEGRAM_API_BASE", fake.url)

    created: list[str] = []
    abandoned: list[str] = []
    listed = [ChangeInfo(id="2026-06-13-q1w2e3", title="Listed",
                         status="running", step="plan")]
    spath = socket_path(tmp_path)
    shutdown = anyio.Event()

    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(functools.partial(
                run_daemon, tmp_path,
                env={"IMP_TELEGRAM_TOKEN": "TOK", "IMP_TELEGRAM_CHAT_ID": "4242"},
                create=created.append,
                abandon=abandoned.append,
                list_changes=lambda: listed,
                resume=lambda cid, text: None,
                install_signals=False, shutdown=shutdown))

            with anyio.fail_after(5):
                while not socket_is_live(spath):
                    await anyio.sleep(0.02)

            # A live south connection emits an event so the adapter records its handle/msg map.
            client = await anyio.connect_unix(str(spath))
            await client.send((make_hello(CHANGE, "Dark mode") + "\n").encode())
            await client.send(json.dumps({
                "type": "awaiting_user_input", "payload": {"prompt": "go?"},
                "change": {"id": CHANGE, "title": "Dark mode"}}).encode() + b"\n")
            await _wait(lambda: _text_in_outbox(fake, "Dark mode"))
            m1 = next(m["message_id"] for m in fake.outbox() if "Dark mode" in m["text"])

            fake.inject("/help")
            await _wait(lambda: _text_in_outbox(fake, HELP_TEXT))

            fake.inject("/new add a foo() helper")
            await _wait(lambda: created == ["add a foo() helper"])
            await _wait(lambda: _text_in_outbox(fake, "Spawning"))

            fake.inject("/list")
            await _wait(lambda: _text_in_outbox(fake, "@q1w2e3  Listed — running · plan"))

            # A threaded reply rides the live south connection back to the connector.
            fake.inject_reply("approved", reply_to_message_id=m1)
            buf = BufferedByteReceiveStream(client)
            with anyio.fail_after(10):
                line = (await buf.receive_until(b"\n", _MAX)).decode("utf-8")
            assert line == "approved"

            # /abandon, addressed by replying to the Change's message.
            fake.inject_reply("/abandon", reply_to_message_id=m1)
            await _wait(lambda: abandoned == [CHANGE])
            await _wait(lambda: _text_in_outbox(fake, "Abandoning @zzzzzz"))

            # An unroutable bare message gets a hint, never a silent drop.
            fake.inject("who is this even")
            await _wait(lambda: _text_in_outbox(fake, ROUTE_HINT))

            await client.aclose()
            shutdown.set()
    finally:
        fake.stop()
