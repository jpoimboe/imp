from __future__ import annotations

from pathlib import Path

import pytest

import imp.cli.main as m
from imp.cli.main import _build_parser, main
from imp.engine import worktree


def _help_for(argv: list[str], capsys: pytest.CaptureFixture[str]) -> str:
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(argv)
    return capsys.readouterr().out


def test_top_level_help_describes_every_subcommand(
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = _help_for(["-h"], capsys)
    assert "Start a new quest" in out
    assert "Resume a paused" in out
    assert "List your quests" in out
    assert "one quest's detail" in out
    assert "effective config" in out


def test_new_help_documents_the_message_positional(
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = _help_for(["new", "-h"], capsys)
    assert "MESSAGE" in out
    assert "opening intent" in out.lower()
    assert "front-door" not in out.lower()
    assert "threshold" not in out.lower()


def test_resume_help_documents_the_id_positional(
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = " ".join(_help_for(["resume", "-h"], capsys).split())
    assert "id of the quest to resume, or any unique part of it." in out
    assert "With no id on a tty, pick from a list of resumable quests." in out


def test_log_help_documents_the_id_positional(
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = _help_for(["log", "-h"], capsys)
    assert "Quest id, or any unique part of it." in out


@pytest.mark.parametrize(("cmd", "needle"), [
    ("resume", "[ID]"),
    ("log", "[ID] [RANGE]"),
    ("events", "[ID] [RANGE]"),
    ("show", "[ID]"),
    ("abandon", "[ID]"),
    ("edit", "[--base BRANCH] ID"),
])
def test_quest_positional_is_named_id_in_usage(
    cmd: str, needle: str, capsys: pytest.CaptureFixture[str],
) -> None:
    usage = " ".join(_help_for([cmd, "-h"], capsys).split("\n\n")[0].split())
    assert usage.startswith(f"usage: imp {cmd} ")
    assert needle in usage
    assert "token" not in usage


def test_edit_without_an_id_names_the_missing_argument_id(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["edit"])
    assert "the following arguments are required: ID" in capsys.readouterr().err


def test_help_lists_model_command() -> None:
    from imp.cli.slash import _help
    out: list[str] = []
    _help(out.append, materialized=False)
    assert any(line.strip().startswith("/model") for line in out)


def test_opening_hint_omits_model() -> None:
    from imp.cli.prompt import DEFAULT_HINT
    assert "/model" not in DEFAULT_HINT


def test_events_help_documents_bounded_event_search(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    out = _help_for(["events", "-h"], capsys)
    assert "subcommands:" in out
    assert "imp events search <query>" in out
    flattened = " ".join(out.split())
    assert "Substring search across event content (tool calls, results, reasoning)." in flattened
    assert "Bounded by default; use --all for exhaustive output. Full flags with:" in flattened


def test_events_help_moves_search_pointer_out_of_the_description(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    # The old "Run imp events search <query> …" sentence is gone from the
    # description; the description still documents the core behaviors.
    monkeypatch.setenv("COLUMNS", "200")
    out = _help_for(["events", "-h"], capsys)
    assert "Run imp events search" not in out
    assert "a scoped index" in out
    assert "--json for structured" in out


def test_events_help_no_longer_promises_a_most_recent_default(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    # AC 8: the events help must not promise that omitting the id targets the
    # most-recent quest.
    monkeypatch.setenv("COLUMNS", "200")
    out = _help_for(["events", "-h"], capsys)
    assert "most recent" not in out


def test_log_help_no_longer_promises_a_most_recent_default(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    # AC 8: the log token help must not promise a most-recent default, but the
    # positional is still documented.
    monkeypatch.setenv("COLUMNS", "200")
    out = _help_for(["log", "-h"], capsys)
    assert "most recent" not in out
    assert "Quest id, or any unique part of it." in out


def test_log_help_says_the_content_filters_reach_the_exports(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("COLUMNS", "80")
    flat = " ".join(_help_for(["log", "-h"], capsys).split())
    assert ("--show/--hide toggle individual content categories (thinking, "
            "tools, subagents, prompts, tiers) over the lean view, in the "
            "terminal and in --md/--html exports alike.") in flat


def test_bare_events_search_is_one_error_line_outside_any_repo(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path,
) -> None:
    # A raising _control_root makes this fail if the guard ever moves after it.
    def no_repo() -> Path:
        raise worktree.PreconditionError("not inside an imp project")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", no_repo)
    rc = main(["events", "search"])
    captured = capsys.readouterr()
    assert rc == 2
    assert captured.err == "imp: events search needs a query — see imp events search -h\n"
    assert captured.out == ""


def test_events_search_short_help_still_prints_full_help(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    with pytest.raises(SystemExit) as exc:
        main(["events", "search", "-h"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "Case-insensitive substring search" in out
    assert "--agent" in out


def test_new_message_help_is_seed_alias(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    out = _help_for(["new", "-h"], capsys)
    assert "Alias for the positional opening message" in out
    assert "seeds the opening step" in out

    chat_out = _help_for(["chat", "-h"], capsys)
    assert "Alias for the positional opening message" in chat_out


def test_resume_help_drops_replay_claim(capsys: pytest.CaptureFixture[str]) -> None:
    out = " ".join(_help_for(["resume", "-h"], capsys).split())
    assert "replaying its narrative" not in out
    assert "continuing the workflow where it stopped" in out


def test_bare_remote_control_prints_full_help_on_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    # Bare `imp remote-control` (no action) prints its full help — listing the
    # start and stop actions — to stderr and exits 2, instead of argparse's
    # terse required-argument error.
    monkeypatch.setenv("COLUMNS", "200")
    rc = main(["remote-control"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "start" in err
    assert "stop" in err


_NEW_DESCRIPTION = (
    "Start a new quest and run the workflow from your opening message. By "
    "default imp scopes the work with you first, and the quest stays a draft "
    "with no branch until you choose to build it. Once you've replied, a draft "
    "you leave stays in your active list, ready for imp resume <id>. Use "
    "--backlog to save an idea for later, or --background to run the quest "
    "detached with no live prompt."
)

_CHAT_DESCRIPTION = (
    "Open a conversation with imp: ask how it works, check on existing work, "
    "or scope a new quest. Nothing is built until you choose to. Once you've "
    "replied, a conversation you leave is archived, still findable with imp "
    "search and resumable with imp resume <id>; if a quest took shape, it "
    "stays in your active list as a draft instead."
)


@pytest.mark.parametrize(
    ("command", "description"),
    [("new", _NEW_DESCRIPTION), ("chat", _CHAT_DESCRIPTION)],
)
def test_new_and_chat_help_print_description_before_positionals(
    command: str, description: str, capsys: pytest.CaptureFixture[str],
) -> None:
    flat = " ".join(_help_for([command, "-h"], capsys).split())
    assert description in flat
    assert flat.index(description) < flat.index("positional arguments:")
