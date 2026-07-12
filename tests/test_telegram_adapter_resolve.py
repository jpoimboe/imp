from __future__ import annotations

from imp.remote.daemon.telegram_adapter import Inbound, resolve_quest_id

MSG_TO_CHANGE = {99: "2026-06-13-aaaaaa"}


def _known(*ids: str):
    known = set(ids)
    return lambda cid: cid in known


def test_native_reply_to_maps_to_its_change_unchanged() -> None:
    item = Inbound(text="approved", reply_to_message_id=99)
    assert resolve_quest_id(item, MSG_TO_CHANGE, _known()) == (
        "2026-06-13-aaaaaa", "approved")


def test_at_handle_resolves_directly_when_quest_is_known() -> None:
    item = Inbound(text="@bbbbbb ship it", reply_to_message_id=None)
    assert resolve_quest_id(item, MSG_TO_CHANGE, _known("bbbbbb")) == ("bbbbbb", "ship it")


def test_at_handle_with_empty_msg_map_still_resolves_post_restart() -> None:
    # AC 3(a): a fresh daemon has an EMPTY _msg_to_quest and no handle map at all;
    # @<id> must still resolve statelessly when the quest is known.
    item = Inbound(text="@bbbbbb go", reply_to_message_id=None)
    assert resolve_quest_id(item, {}, _known("bbbbbb")) == ("bbbbbb", "go")


def test_at_handle_unknown_quest_returns_none_with_full_text() -> None:
    # AC 3(b): is_known_quest false -> (None, text) -> Notice(ROUTE_HINT) via parse.
    item = Inbound(text="@xxxxxx help", reply_to_message_id=None)
    assert resolve_quest_id(item, {}, _known("bbbbbb")) == (None, "@xxxxxx help")


def test_reply_to_takes_precedence_over_an_at_handle_in_the_text() -> None:
    item = Inbound(text="@bbbbbb ship it", reply_to_message_id=99)
    assert resolve_quest_id(item, MSG_TO_CHANGE, _known("bbbbbb")) == (
        "2026-06-13-aaaaaa", "@bbbbbb ship it")


def test_unknown_reply_to_falls_through_to_handle_resolution() -> None:
    item = Inbound(text="@bbbbbb go", reply_to_message_id=12345)
    assert resolve_quest_id(item, MSG_TO_CHANGE, _known("bbbbbb")) == ("bbbbbb", "go")


def test_unresolvable_reply_returns_none_with_text_intact() -> None:
    item = Inbound(text="who?", reply_to_message_id=None)
    assert resolve_quest_id(item, MSG_TO_CHANGE, _known()) == (None, "who?")
