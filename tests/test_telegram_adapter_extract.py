from __future__ import annotations

from imp.remote.daemon.telegram_adapter import Inbound, extract_inbound

CHAT = 4242


def _update(uid: int, text: str, *, reply_to: int | None = None) -> dict:
    msg: dict = {"chat": {"id": CHAT}, "text": text}
    if reply_to is not None:
        msg["reply_to_message"] = {"message_id": reply_to}
    return {"update_id": uid, "message": msg}


def test_extract_advances_offset_past_the_highest_update_id() -> None:
    resp = {"result": [_update(10, "a"), _update(11, "b")]}
    _, new_offset = extract_inbound(resp, CHAT, offset=0)
    assert new_offset == 12


def test_extract_keeps_text_and_reply_to_message_id() -> None:
    resp = {"result": [_update(10, "approved", reply_to=99)]}
    items, _ = extract_inbound(resp, CHAT, offset=0)
    assert items == [Inbound(text="approved", reply_to_message_id=99)]


def test_extract_drops_messages_from_other_chats() -> None:
    foreign = {"update_id": 10, "message": {"chat": {"id": 1}, "text": "x"}}
    items, new_offset = extract_inbound({"result": [foreign]}, CHAT, offset=0)
    assert items == []
    assert new_offset == 11  # offset still advances past the skipped update


def test_extract_reply_to_is_none_when_absent() -> None:
    items, _ = extract_inbound({"result": [_update(10, "hi")]}, CHAT, offset=0)
    assert items == [Inbound(text="hi", reply_to_message_id=None)]


def test_extract_drops_a_message_with_no_text_field() -> None:
    update = {"update_id": 10, "message": {"chat": {"id": CHAT}}}  # no "text" key
    items, new_offset = extract_inbound({"result": [update]}, CHAT, offset=0)
    assert items == []
    assert new_offset == 11
