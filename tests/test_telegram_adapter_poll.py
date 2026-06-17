from __future__ import annotations

from imp.remote.daemon.commands import ROUTE_HINT, Notice, Reply
from imp.remote.daemon.focus import Focus
from imp.remote.daemon.telegram_adapter import TelegramNorthAdapter

CHAT = 4242
EVENT = {
    "type": "awaiting_user_input",
    "payload": {"prompt": "go?"},
    "change": {"id": "k3f9qa", "title": "Dark mode"},
}


class FakeUpdates:
    """Records the offset each getUpdates call received; pops canned responses."""

    def __init__(self, responses: list[dict]) -> None:
        self._responses = list(responses)
        self.offsets: list[int] = []

    def __call__(self, offset: int) -> dict:
        self.offsets.append(offset)
        return self._responses.pop(0) if self._responses else {"result": []}


def _reply_update(uid: int, text: str, reply_to: int) -> dict:
    return {"update_id": uid, "message": {
        "chat": {"id": CHAT}, "text": text,
        "reply_to_message": {"message_id": reply_to}}}


def test_poll_routes_a_native_reply_as_a_reply_command() -> None:
    submitted: list = []
    get_updates = FakeUpdates([{"result": [_reply_update(10, "approved", 42)]}])
    focus = Focus()
    adapter = TelegramNorthAdapter(
        chat_id=CHAT, submit=submitted.append,
        send=lambda text, silent: 42, get_updates=get_updates, focus=focus)
    adapter.on_event(EVENT)          # records message_id 42 -> the Change
    adapter.poll_once()
    assert submitted == [Reply("k3f9qa", "approved")]


def test_poll_advances_the_offset_across_cycles() -> None:
    get_updates = FakeUpdates([{"result": [_reply_update(10, "x", 42)]}])
    adapter = TelegramNorthAdapter(
        chat_id=CHAT, submit=lambda command: None,
        send=lambda text, silent: 42, get_updates=get_updates, focus=Focus())
    adapter.poll_once()              # consumes update 10
    adapter.poll_once()              # next cycle must start past it
    assert get_updates.offsets == [0, 11]


def test_poll_submits_a_hint_for_an_unresolvable_inbound() -> None:
    submitted: list = []
    # update 10 replies to message 999, which was never sent -> unresolvable.
    get_updates = FakeUpdates([{"result": [_reply_update(10, "huh", 999)]}])
    adapter = TelegramNorthAdapter(
        chat_id=CHAT, submit=submitted.append,
        send=lambda text, silent: 42, get_updates=get_updates, focus=Focus())
    adapter.poll_once()
    assert submitted == [Notice(ROUTE_HINT)]   # no inbound is silently dropped


def test_poll_routes_an_at_handle_reply_as_a_reply_command() -> None:
    submitted: list = []
    update = {"update_id": 10, "message": {"chat": {"id": CHAT}, "text": "@k3f9qa go"}}
    get_updates = FakeUpdates([{"result": [update]}])
    focus = Focus()
    adapter = TelegramNorthAdapter(
        chat_id=CHAT, submit=submitted.append,
        send=lambda text, silent: 42, get_updates=get_updates, focus=focus)
    adapter.on_event(EVENT)          # records handle k3f9qa -> the Change
    adapter.poll_once()
    assert submitted == [Reply("k3f9qa", "go")]
