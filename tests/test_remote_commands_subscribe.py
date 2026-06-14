from __future__ import annotations

from imp.remote.daemon.commands import (
    ROUTE_HINT,
    Notice,
    Replay,
    Subscribe,
    Unsubscribe,
    parse,
)
from imp.remote.daemon.telegram_adapter import parse_handle

A = "2026-06-13-aaaaaa"
B = "2026-06-13-bbbbbb"


def fake_resolve(text: str, reply_to: int | None) -> tuple[str | None, str]:
    if reply_to == 42:
        return (A, text)
    handle, rest = parse_handle(text)
    if handle == "good":
        return (B, rest)
    return (None, text)


def test_subscribe_reply_target() -> None:
    assert parse("/subscribe", 42, fake_resolve) == Subscribe(A)


def test_sub_alias_at_handle_target() -> None:
    assert parse("/sub @good", None, fake_resolve) == Subscribe(B)


def test_subscribe_unroutable_is_route_hint() -> None:
    assert parse("/subscribe", None, fake_resolve) == Notice(ROUTE_HINT)


def test_unsubscribe_and_aliases_parse_to_unsubscribe() -> None:
    assert parse("/unsubscribe", None, fake_resolve) == Unsubscribe()
    assert parse("/unsub", None, fake_resolve) == Unsubscribe()
    assert parse("/mute", None, fake_resolve) == Unsubscribe()


def test_replay_defaults_n_to_5_and_keeps_target() -> None:
    assert parse("/replay", 42, fake_resolve) == Replay(A, 5)


def test_replay_parses_explicit_n() -> None:
    assert parse("/replay 3", 42, fake_resolve) == Replay(A, 3)


def test_replay_clamps_n_to_1_20() -> None:
    assert parse("/replay 99", 42, fake_resolve) == Replay(A, 20)
    assert parse("/replay 0", 42, fake_resolve) == Replay(A, 1)


def test_replay_at_handle_with_count() -> None:
    assert parse("/replay @good 4", None, fake_resolve) == Replay(B, 4)


def test_replay_at_handle_without_count_defaults_5() -> None:
    assert parse("/replay @good", None, fake_resolve) == Replay(B, 5)


def test_replay_no_target_yields_none_change_id() -> None:
    assert parse("/replay 7", None, fake_resolve) == Replay(None, 7)


def test_replay_no_args_no_target_yields_none_n5() -> None:
    assert parse("/replay", None, fake_resolve) == Replay(None, 5)
