from __future__ import annotations

from imp.remote.daemon.commands import (
    ROUTE_HINT,
    UNSUBSCRIBE_USAGE,
    Notice,
    Replay,
    Subscribe,
    Unsubscribe,
    parse,
)
from imp.remote.daemon.telegram_adapter import parse_handle, parse_subscribe_handle

A = "2026-06-13-aaaaaa"
B = "2026-06-13-bbbbbb"


def fake_resolve(text: str, reply_to: int | None) -> tuple[str | None, str]:
    if reply_to == 42:
        return (A, text)
    handle, rest = parse_handle(text)
    if handle == "good":
        return (B, rest)
    return (None, text)


def test_parse_subscribe_handle_returns_the_handle_as_quest_id() -> None:
    assert parse_subscribe_handle("@zzzzzz") == "zzzzzz"


def test_parse_subscribe_handle_none_without_an_at_handle() -> None:
    assert parse_subscribe_handle("") is None
    assert parse_subscribe_handle("nope") is None


def test_subscribe_resolves_by_at_handle_only() -> None:
    assert parse(
        "/subscribe @zzzzzz", None, fake_resolve, parse_subscribe_handle
    ) == Subscribe("zzzzzz")


def test_sub_alias_resolves_by_at_handle() -> None:
    assert parse(
        "/sub @abc123", None, fake_resolve, parse_subscribe_handle
    ) == Subscribe("abc123")


def test_subscribe_with_no_handle_is_route_hint() -> None:
    assert parse(
        "/subscribe", None, fake_resolve, parse_subscribe_handle
    ) == Notice(ROUTE_HINT)


def test_subscribe_reply_to_is_not_a_subscribe_target() -> None:
    # reply_to 42 resolves a Change for reply/abandon, but never for subscribe.
    assert parse(
        "/subscribe", 42, fake_resolve, parse_subscribe_handle
    ) == Notice(ROUTE_HINT)


def test_unsubscribe_resolves_a_reply_to_target() -> None:
    assert parse("/unsubscribe", 42, fake_resolve) == Unsubscribe(A)


def test_unsubscribe_resolves_an_at_handle_target() -> None:
    assert parse("/unsub @good", None, fake_resolve) == Unsubscribe(B)
    assert parse("/mute @good", None, fake_resolve) == Unsubscribe(B)


def test_unsubscribe_with_no_target_is_a_usage_notice() -> None:
    assert parse("/unsubscribe", None, fake_resolve) == Notice(UNSUBSCRIBE_USAGE)


def test_replay_defaults_n_to_5_and_keeps_target() -> None:
    assert parse("/replay", 42, fake_resolve) == Replay(A, 5)


def test_replay_parses_explicit_n() -> None:
    assert parse("/replay 3", 42, fake_resolve) == Replay(A, 3)


def test_replay_clamps_n_to_1_20() -> None:
    assert parse("/replay 99", 42, fake_resolve) == Replay(A, 20)
    assert parse("/replay 0", 42, fake_resolve) == Replay(A, 1)


def test_replay_at_handle_with_count() -> None:
    assert parse("/replay @good 4", None, fake_resolve) == Replay(B, 4)


def test_replay_no_target_yields_none_quest_id() -> None:
    assert parse("/replay 7", None, fake_resolve) == Replay(None, 7)


def test_replay_at_handle_without_count_defaults_to_5() -> None:
    assert parse("/replay @good", None, fake_resolve) == Replay(B, 5)


def test_replay_with_no_args_and_no_target_yields_none_and_5() -> None:
    assert parse("/replay", None, fake_resolve) == Replay(None, 5)
