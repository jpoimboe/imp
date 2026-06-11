from __future__ import annotations

from imp.remote.drivers import telegram as imp_telegram


def _update(update_id: int, chat_id: int, text: str | None) -> dict:
    msg: dict = {"chat": {"id": chat_id}}
    if text is not None:
        msg["text"] = text
    return {"update_id": update_id, "message": msg}


def test_collects_text_from_allowlisted_chat_and_advances_offset() -> None:
    response = {"result": [_update(10, 5, "hello"), _update(11, 5, "world")]}
    replies, new_offset = imp_telegram.extract_replies(response, chat_id=5, offset=0)
    assert replies == ["hello", "world"]
    # Offset advances past the highest update_id so the next poll never re-fetches.
    assert new_offset == 12


def test_foreign_chat_is_filtered_but_offset_still_advances() -> None:
    response = {"result": [_update(7, 999, "from a stranger")]}
    replies, new_offset = imp_telegram.extract_replies(response, chat_id=5, offset=0)
    assert replies == []          # allowlist drops the foreign chat
    assert new_offset == 8        # but the update is acknowledged


def test_non_text_update_is_skipped_but_acknowledged() -> None:
    response = {"result": [_update(3, 5, None)]}  # e.g. a photo, no text
    replies, new_offset = imp_telegram.extract_replies(response, chat_id=5, offset=0)
    assert replies == []
    assert new_offset == 4


def test_empty_result_leaves_offset_unchanged() -> None:
    replies, new_offset = imp_telegram.extract_replies({"result": []}, chat_id=5, offset=50)
    assert replies == []
    assert new_offset == 50
