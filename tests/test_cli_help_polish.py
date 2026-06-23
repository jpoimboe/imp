from __future__ import annotations

import argparse

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


def test_ls_has_description() -> None:
    sp = _subparser("ls")
    assert "Backlog" in sp.format_help()
    assert sp.description is not None


def test_ls_aliases_resolve_to_the_same_handler() -> None:
    from imp.cli.main import _build_parser, _cmd_status
    parser = _build_parser()
    for name in ("ls", "list"):
        assert parser.parse_args([name]).func is _cmd_status


def test_config_has_description() -> None:
    desc = _subparser("config").description
    assert desc is not None
    assert "provenance" in desc
