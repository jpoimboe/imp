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
    assert "which quest to abandon" in help_text


def test_list_documents_overview() -> None:
    sp = _subparser("list")
    help_text = sp.format_help()
    assert "backlog" in help_text
    assert sp.description is not None


def test_show_documents_detail_only() -> None:
    sp = _subparser("show")
    help_text = sp.format_help()
    assert "backlog" not in help_text          # no overview language on show
    assert "detail card" in help_text
    assert sp.description is not None


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


def test_edit_documents_base_flag() -> None:
    sp = _subparser("edit")
    help_text = " ".join(sp.format_help().split())
    assert ("Re-base an un-materialized quest on an existing BRANCH. Rejected "
            "once the quest has been built.") in help_text
    assert sp.description is not None and "base branch" in sp.description
    # The subparser's own one-line help (as `imp --help` lists it) names it too.
    assert "base branch" in " ".join(_build_parser().format_help().split())


def test_log_follow_help_documents_content_flag_composition() -> None:
    help_text = " ".join(_subparser("log").format_help().split())
    assert ("Live-tail the narrative (tail -f): replay it, then stream new events "
            "until the quest reaches a terminal status. Composes with "
            "--full/--show/--hide; implies --no-pager.") in help_text


def test_show_documents_the_verify_artifact() -> None:
    sp = _subparser("show")
    assert " ".join((sp.description or "").split()) == (
        "Print a quest's detail card, or name an artifact to print just that: "
        "spec for the spec, verify for the last failing test-gate run. Reads "
        "the live quest files while a quest is in flight and falls back to the "
        "archive once it is finished."
    )
    assert "{spec,verify}" in sp.format_help()
