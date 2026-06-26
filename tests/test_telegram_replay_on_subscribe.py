from __future__ import annotations

import os
import queue
import time

import pytest

from imp.remote.daemon.commands import Notice, Subscribe
from imp.remote.daemon.runner import _handle_command
from imp.remote.daemon.subscriptions import Subscriptions
from imp.remote.daemon.telegram_adapter import TelegramNorthAdapter


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _utc():
    saved = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()
    yield
    if saved is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = saved
    time.tzset()


class _Control:
    def __init__(self, subscriptions: Subscriptions, history: list[dict]) -> None:
        self.subscriptions = subscriptions
        self._history = history

    def snapshot(self, change_id: str) -> tuple[str, str, str]:
        return ("running", "plan", "T")

    def full_history(self, change_id: str) -> list[dict]:
        return self._history


def _event(event_id: str, etype: str, payload: dict) -> dict:
    return {
        "id": event_id, "type": etype, "timestamp": "2026-06-25T14:32:05+00:00",
        "change": {"id": "c", "title": "T"}, "payload": payload,
    }


def _drain(q: queue.Queue) -> list:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


@pytest.mark.anyio
async def test_subscribe_replays_in_order_then_confirms_and_dedups_live() -> None:
    subs = Subscriptions()
    history = [
        _event("e1", "notice", {"text": "one"}),
        _event("e2", "agent_output", {"text": "two"}),
    ]
    control = _Control(subs, history)
    responses: queue.Queue = queue.Queue()

    await _handle_command(Subscribe("c"), control, responses)

    msgs = _drain(responses)
    assert msgs[0].text == "c · T · 2:32pm\none\n↩ @c"
    assert msgs[1].text == "c · T · 2:32pm\ntwo\n↩ @c"
    assert msgs[-1] == Notice("Following @c.")
    assert subs.contains("c")
    assert subs.seen("c", "e1") and subs.seen("c", "e2")

    # The live relay re-delivers the same on-disk events; an id in the replayed
    # set is suppressed (no duplicate), a fresh id streams silently.
    sent: list[tuple[str, bool]] = []
    adapter = TelegramNorthAdapter(
        chat_id=1, submit=lambda _c: None,
        send=lambda text, silent: (sent.append((text, silent)) or 1),
        get_updates=lambda _o: {"result": []}, subscriptions=subs)
    adapter.on_event(_event("e1", "notice", {"text": "one"}))   # already replayed
    assert sent == []
    adapter.on_event(_event("e9", "notice", {"text": "live"}))  # new → streams
    assert len(sent) == 1 and sent[0][1] is True


@pytest.mark.anyio
async def test_subscribe_with_empty_history_just_confirms() -> None:
    subs = Subscriptions()
    control = _Control(subs, [])
    responses: queue.Queue = queue.Queue()
    await _handle_command(Subscribe("c"), control, responses)
    assert _drain(responses) == [Notice("Following @c.")]
    assert subs.contains("c")
