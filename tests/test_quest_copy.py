from __future__ import annotations

from pathlib import Path

import pytest

from imp.cli.main import _build_parser

_ASSETS = Path("src/imp/assets/workflows/imp")


def _help_for(cmd: str, capsys) -> str:
    with pytest.raises(SystemExit):
        _build_parser().parse_args([cmd, "--help"])
    # argparse hard-wraps help at the terminal width (80 under pytest's piped
    # fd), so collapse all whitespace before asserting — otherwise multi-word
    # phrases straddle a wrap boundary and the `in` check false-fails.
    return " ".join(capsys.readouterr().out.split())


def test_show_help_names_quests_not_changes(capsys) -> None:
    # `imp <cmd> --help` prints the subparser DESCRIPTION + arg helps, NOT the
    # parent-list `add_parser(help=...)` string — so assert against those.
    out = _help_for("show", capsys)
    assert "Print a Quest's detail card" in out            # description=
    assert "while a Quest is in flight" in out             # description=
    assert "Quest id or its short token" in out            # token arg help
    assert "change" not in out


def test_search_help_names_quests(capsys) -> None:
    out = _help_for("search", capsys)
    assert "backlog, and archived Quests" in out           # description=
    assert "a Quest's id, title, or conversation content" in out   # description=
    assert "change" not in out


def test_overview_command_list_names_quests() -> None:
    from imp.cli.main import _OVERVIEW_COMMANDS
    descs = " | ".join(d for _verb, d in _OVERVIEW_COMMANDS)
    assert "scope a Quest." in descs
    assert "Scope a new Quest in conversation" in descs
    assert "List your Quests: in-flight, backlog, and recently merged." in descs
    assert "Show one Quest's detail or a named artifact (imp show <id>)." in descs
    assert "change" not in descs


def test_slash_help_listing_names_quests() -> None:
    from imp.cli.slash import _help
    lines: list[str] = []
    _help(lines.append)
    text = "\n".join(lines)
    assert "List your Quests (-a adds completed/abandoned)" in text
    assert "Search archived Quests by content, title, or id" in text
    assert "Abandon the current Quest" in text
    assert "change" not in text


def test_tool_descriptions_name_quest() -> None:
    from imp.agents.tools import get_registry
    reg = get_registry()
    # positive literals only — the descriptions contain unrelated substrings like
    # "head_changed", so a blanket `"change" not in` check would false-fail.
    assert reg["query_events"].description.startswith("The Quest's event log:")
    assert "did on this Quest." in reg["query_events"].description
    assert "another Quest's timeline" in reg["query_events"].description
    assert "list quest ids it depends_on" in reg["create_quest"].description
    assert "switch this Quest into it" in reg["create_and_enter_worktree"].description
    assert "; the Quest's " in reg["glob"].description
    assert "; the Quest's " in reg["grep"].description


def test_mental_model_primer_names_quest() -> None:
    text = (_ASSETS / "skills/agent/imp-mental-model.md").read_text()
    assert "## Quest and its home" in text
    assert "A **Quest** is one unit of work imp tracks from idea to merge." in text
    assert "per-quest home on disk at " in text
    assert "**Change**" not in text
    assert "## Change and its home" not in text


def test_reviewer_prompts_label_quest_title() -> None:
    for name in ("agents/plan-reviewer.md", "agents/spec-reviewer.md"):
        text = (_ASSETS / name).read_text()
        assert "Quest title (user's starting intent): `{{ change.title }}`" in text
        assert "Change title" not in text


def test_finalize_prompt_defers_to_backlog_quest() -> None:
    text = (_ASSETS / "agents/finalize.md").read_text()
    assert "group related items into one Quest" in text
    assert "hand it to a new backlog Quest." in text
    assert "backlog Change" not in text
