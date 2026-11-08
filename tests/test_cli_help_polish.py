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


def test_abandon_documents_id_positional() -> None:
    help_text = _subparser("abandon").format_help()
    assert "id of the quest to abandon" in help_text


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
    assert ("Re-base a quest on an existing BRANCH. Rejected "
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
        "spec for the spec, verify for the output of the last failed test run. "
        "Reads the live quest files while a quest is in flight and falls back "
        "to the archive once it is finished."
    )
    assert "{spec,verify}" in sp.format_help()


def _walk_subparsers(
    parser: argparse.ArgumentParser, path: tuple[str, ...] = (),
) -> list[tuple[tuple[str, ...], argparse.ArgumentParser]]:
    found: list[tuple[tuple[str, ...], argparse.ArgumentParser]] = []
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                found.append(((*path, name), sub))
                found.extend(_walk_subparsers(sub, (*path, name)))
    return found


def test_every_subparser_has_a_description() -> None:
    missing = [" ".join(path) for path, sub in _walk_subparsers(_build_parser())
               if not (sub.description or "").strip()]
    assert not missing, f"subparsers without a description: {missing}"


def _nested(*path: str) -> argparse.ArgumentParser:
    return dict(_walk_subparsers(_build_parser()))[path]


@pytest.mark.parametrize(("path", "description"), [
    (("help",),
     "Print the top-level command list, or with <command>, that command's full "
     "help (same as 'imp <command> -h')."),
    (("config", "set"),
     "Write one setting to a config file. Without a scope flag, the value goes "
     "to the file that setting normally lives in; --project, --global, or "
     "--local picks the file explicitly. Run 'imp config' to see the result "
     "and where each value comes from."),
    (("config", "unset"),
     "Comment out one setting in a config file so the next layer down takes "
     "effect. Without a scope flag, it edits the file that setting normally "
     "lives in; --project, --global, or --local picks the file explicitly."),
    (("remote-control", "start"),
     "Start this repo's remote-control daemon in the foreground; it relays "
     "every quest's activity to the configured chat transport until stopped."),
    (("remote-control", "stop"),
     "Stop this repo's running remote-control daemon by sending it SIGTERM."),
])
def test_subcommand_description_printed_after_usage(
    path: tuple[str, ...], description: str,
) -> None:
    sub = _nested(*path)
    assert " ".join((sub.description or "").split()) == description
    flat = " ".join(sub.format_help().split())
    assert flat.startswith("usage:")
    assert description in flat
    sections = [i for i in (flat.find("positional arguments:"),
                            flat.find("options:")) if i != -1]
    assert flat.index(description) < min(sections)


def test_help_command_positional_has_help_text() -> None:
    flat = " ".join(_subparser("help").format_help().split())
    assert "<command> Command to show help for." in flat


def test_new_title_flag_help_says_the_opening_step_sets_one() -> None:
    help_text = " ".join(_subparser("new").format_help().split())
    assert ("Set the quest title explicitly; the workflow's opening step "
            "sets one if omitted.") in help_text
