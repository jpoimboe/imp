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


def test_list_documents_overview() -> None:
    sp = _subparser("list")
    help_text = sp.format_help()
    assert "Backlog" in help_text
    assert sp.description is not None


def test_show_documents_detail_only() -> None:
    sp = _subparser("show")
    help_text = sp.format_help()
    assert "Backlog" not in help_text          # no overview language on show
    assert "detail card" in help_text
    assert sp.description is not None


def test_list_and_show_both_resolve() -> None:
    from imp.cli.main import _build_parser, _cmd_list, _cmd_show
    parser = _build_parser()
    assert parser.parse_args(["list"]).func is _cmd_list
    assert parser.parse_args(["show", "liveid"]).func is _cmd_show


def test_config_has_description() -> None:
    desc = _subparser("config").description
    assert desc is not None
    assert "provenance" in desc


def test_resume_has_description() -> None:
    desc = _subparser("resume").description
    assert desc is not None
    assert "resumable" in desc


def test_abandon_has_description() -> None:
    desc = _subparser("abandon").description
    assert desc is not None
    assert "worktree" in desc


def test_edit_has_description() -> None:
    desc = _subparser("edit").description
    assert desc is not None
    assert "title" in desc
