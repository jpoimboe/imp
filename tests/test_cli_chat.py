from __future__ import annotations

from imp.cli.main import _build_parser, _cmd_chat, _command_names


def test_chat_is_a_subcommand() -> None:
    assert "chat" in _command_names(_build_parser())


def test_chat_takes_an_optional_opening_message() -> None:
    args = _build_parser().parse_args(["chat", "scope a dark mode toggle"])
    assert args.opening == "scope a dark mode toggle"
    assert args.func is _cmd_chat


def test_chat_opening_is_optional() -> None:
    args = _build_parser().parse_args(["chat"])
    assert args.opening is None
    assert args.func is _cmd_chat
