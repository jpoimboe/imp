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


def test_show_documents_overview_and_detail() -> None:
    sp = _subparser("show")
    help_text = sp.format_help()
    assert "Backlog" in help_text
    assert sp.description is not None


def test_show_resolves_and_list_is_gone() -> None:
    from imp.cli.main import _build_parser, _cmd_show
    parser = _build_parser()
    assert parser.parse_args(["show"]).func is _cmd_show
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["list"])
    assert exc.value.code == 2


def test_config_has_description() -> None:
    desc = _subparser("config").description
    assert desc is not None
    assert "provenance" in desc
