from __future__ import annotations

import argparse

import pytest

from imp.cli.main import _build_parser


def _subparser(name: str) -> argparse.ArgumentParser:
    parser = _build_parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices[name]
    raise AssertionError("no subparsers")


def test_abandon_documents_token_positional() -> None:
    help_text = _subparser("abandon").format_help()
    assert "which Change to abandon" in help_text


def test_list_has_description() -> None:
    sp = _subparser("list")
    assert "Backlog" in sp.format_help()
    assert sp.description is not None


def test_list_resolves_and_ls_is_gone() -> None:
    from imp.cli.main import _build_parser, _cmd_status
    parser = _build_parser()
    assert parser.parse_args(["list"]).func is _cmd_status
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["ls"])
    assert exc.value.code == 2


def test_config_has_description() -> None:
    desc = _subparser("config").description
    assert desc is not None
    assert "provenance" in desc
