from __future__ import annotations

from imp.remote.daemon.telegram_adapter import Inbound, resolve_change_id

MSG_TO_CHANGE = {99: "2026-06-13-aaaaaa"}
HANDLE_TO_CHANGE = {"bbbbbb": "2026-06-13-bbbbbb"}


def test_native_reply_to_maps_to_its_change_unchanged() -> None:
    item = Inbound(text="approved", reply_to_message_id=99)
    assert resolve_change_id(item, MSG_TO_CHANGE, HANDLE_TO_CHANGE) == (
        "2026-06-13-aaaaaa", "approved")


def test_at_handle_fallback_maps_and_strips_the_handle() -> None:
    item = Inbound(text="@bbbbbb ship it", reply_to_message_id=None)
    assert resolve_change_id(item, MSG_TO_CHANGE, HANDLE_TO_CHANGE) == (
        "2026-06-13-bbbbbb", "ship it")


def test_reply_to_takes_precedence_over_an_at_handle_in_the_text() -> None:
    item = Inbound(text="@bbbbbb ship it", reply_to_message_id=99)
    assert resolve_change_id(item, MSG_TO_CHANGE, HANDLE_TO_CHANGE) == (
        "2026-06-13-aaaaaa", "@bbbbbb ship it")


def test_unresolvable_reply_returns_none_with_text_intact() -> None:
    item = Inbound(text="who?", reply_to_message_id=None)
    assert resolve_change_id(item, MSG_TO_CHANGE, HANDLE_TO_CHANGE) == (None, "who?")


def test_unknown_reply_to_falls_through_to_handle_parsing() -> None:
    item = Inbound(text="@bbbbbb go", reply_to_message_id=12345)
    assert resolve_change_id(item, MSG_TO_CHANGE, HANDLE_TO_CHANGE) == (
        "2026-06-13-bbbbbb", "go")


def test_unknown_handle_returns_none_with_full_text_intact() -> None:
    item = Inbound(text="@xxxxxx help", reply_to_message_id=None)
    assert resolve_change_id(item, MSG_TO_CHANGE, HANDLE_TO_CHANGE) == (None, "@xxxxxx help")
