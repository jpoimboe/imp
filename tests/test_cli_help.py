from __future__ import annotations

import pytest

from imp.cli.main import _build_parser, main


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


def test_resume_help_documents_the_token_positional(
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = _help_for(["resume", "-h"], capsys)
    assert "Quest id or resume token" in out


def test_log_help_documents_the_token_positional(
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = _help_for(["log", "-h"], capsys)
    assert "Quest id or its short token" in out


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
    assert "Quest id or its short token" in out


def test_log_help_says_the_content_filters_reach_the_exports(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("COLUMNS", "80")
    flat = " ".join(_help_for(["log", "-h"], capsys).split())
    assert ("--show/--hide toggle individual content categories (thinking, "
            "tools, subagents, prompts, tiers) over the lean view, in the "
            "terminal and in --md/--html exports alike.") in flat


def test_bare_events_search_prints_full_help_on_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    # The no-query guard precedes _control_root(), so no repo is needed.
    monkeypatch.setenv("COLUMNS", "200")
    rc = main(["events", "search"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "Case-insensitive substring search" in err
    assert "--active" in err
    assert "--agent" in err


def test_new_message_help_is_seed_alias(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    out = _help_for(["new", "-h"], capsys)
    assert "Requires --headless" not in out
    assert "Alias for the positional opening message" in out
    assert "seeds the opening step" in out
    assert "supply the opening message via the positional argument or --message" in out

    chat_out = _help_for(["chat", "-h"], capsys)
    assert "Alias for the positional opening message" in chat_out
    assert "supply the opening message via the positional argument or --message" in chat_out


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
