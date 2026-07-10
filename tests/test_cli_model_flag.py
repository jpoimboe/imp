from __future__ import annotations

import pytest

import imp.cli.main as main


def test_new_parser_accepts_model_flag() -> None:
    args = main._build_parser().parse_args(
        ["new", "do a thing", "--model", "anthropic:claude-fable-5"])
    assert args.model == "anthropic:claude-fable-5"


def test_new_parser_accepts_bare_model_flag() -> None:
    args = main._build_parser().parse_args(
        ["new", "do a thing", "--model", "claude-fable-5"])
    assert args.model == "claude-fable-5"


def test_chat_parser_accepts_model_flag() -> None:
    args = main._build_parser().parse_args(["chat", "--model", "anthropic:claude-fable-5"])
    assert args.model == "anthropic:claude-fable-5"


def test_new_parser_model_defaults_none() -> None:
    args = main._build_parser().parse_args(["new", "do a thing"])
    assert args.model is None


def test_chat_parser_model_defaults_none() -> None:
    args = main._build_parser().parse_args(["chat"])
    assert args.model is None


def test_new_parser_rejects_malformed_model_at_parse_time() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main._build_parser().parse_args(["new", "x", "--model", "anthropic:"])
    assert exc_info.value.code == 2


def test_chat_parser_rejects_malformed_model_at_parse_time() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main._build_parser().parse_args(["chat", "--model", ":bogus"])
    assert exc_info.value.code == 2
