from __future__ import annotations

from imp.remote.daemon.focus import Focus
from imp.remote.daemon.telegram_adapter import Inbound, TelegramNorthAdapter

EVENT = {
    "type": "awaiting_user_input",
    "payload": {"prompt": "Need your call"},
    "change": {"id": "2026-06-13-k3f9qa", "title": "Dark mode"},
}


def _adapter(sent: list, *, message_id: int | None = 42, focus: Focus | None = None) -> TelegramNorthAdapter:
    def send(text: str, silent: bool) -> int | None:
        sent.append((text, silent))
        return message_id

    def get_updates(offset: int) -> dict:
        return {"result": []}

    return TelegramNorthAdapter(
        chat_id=4242, submit=lambda cid, text: None,
        send=send, get_updates=get_updates, focus=focus if focus is not None else Focus())


def test_on_event_sends_a_message_carrying_the_handle() -> None:
    sent: list = []
    adapter = _adapter(sent)
    adapter.on_event(EVENT)
    assert len(sent) == 1
    text, silent = sent[0]
    assert "k3f9qa" in text and "Need your call" in text
    assert silent is False   # awaiting_user_input is a loud event


def test_on_event_records_reply_to_mapping_so_a_reply_resolves_home() -> None:
    adapter = _adapter([])
    adapter.on_event(EVENT)
    reply = Inbound(text="yes do it", reply_to_message_id=42)
    assert adapter.resolve(reply) == ("2026-06-13-k3f9qa", "yes do it")


def test_on_event_records_handle_mapping_for_the_at_fallback() -> None:
    adapter = _adapter([])
    adapter.on_event(EVENT)
    reply = Inbound(text="@k3f9qa go", reply_to_message_id=None)
    assert adapter.resolve(reply) == ("2026-06-13-k3f9qa", "go")


def test_on_event_drops_a_silent_event_with_an_empty_body() -> None:
    sent: list = []
    focus = Focus()
    focus.set("2026-06-13-k3f9qa")   # focus matches the event — only empty-body guard drops it
    adapter = _adapter(sent, focus=focus)
    adapter.on_event({"type": "llm_response", "payload": {},
                      "change": {"id": "2026-06-13-k3f9qa", "title": "X"}})
    assert sent == []   # curated silent but empty body -> nothing sent or recorded


def test_on_event_records_handle_map_even_when_send_returns_none() -> None:
    adapter = _adapter([], message_id=None)
    adapter.on_event(EVENT)
    reply = Inbound(text="@k3f9qa go", reply_to_message_id=None)
    assert adapter.resolve(reply) == ("2026-06-13-k3f9qa", "go")
