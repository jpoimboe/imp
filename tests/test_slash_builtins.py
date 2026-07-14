from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from imp.cli.slash import REPROMPT, handle_slash_line
from imp.engine.quest import Quest
from tests.conftest import _seed_control


def test_ls_is_unknown_after_rename(tmp_path: Path) -> None:
    # `/ls` matches no builtin: it falls through the skill lookup and, with no
    # `ls` skill present, is submitted verbatim rather than erroring.
    quest = SimpleNamespace(control_root=tmp_path, workflow_dir=tmp_path)
    out: list[str] = []
    result = handle_slash_line("/ls", quest=quest, output_fn=out.append)
    assert result == "/ls"
    assert out == []


def test_removed_builtins_fall_through_as_ordinary_input(tmp_path: Path) -> None:
    # /list, /search, /resume are no longer builtins: each falls through the
    # skill resolver, matches no skill, and is submitted verbatim as input.
    quest = SimpleNamespace(workflow_dir=tmp_path, control_root=tmp_path)
    for line in ("/list", "/search dark", "/resume conv1"):
        out: list[str] = []
        assert handle_slash_line(line, quest=quest, output_fn=out.append) == line
        assert out == []


def test_help_omits_removed_builtins() -> None:
    from imp.cli.slash import _help
    lines: list[str] = []
    _help(lines.append, materialized=False)
    joined = "\n".join(lines)
    assert "/list" not in joined
    assert "/search" not in joined
    assert "/resume" not in joined


def test_abandon_builtin_returns_literal_for_the_gate_loop(tmp_path: Path) -> None:
    # /abandon is a built-in (NOT a skill): it returns the literal so the session
    # gate loop (cli/session_terminal) confirms and sends CONTROL_ABANDON.
    quest = SimpleNamespace(control_root=tmp_path)
    out: list[str] = []
    result = handle_slash_line("/abandon", quest=quest, output_fn=out.append)
    assert result == "/abandon"
    assert not any("no such skill" in line for line in out)


def test_abandon_is_in_the_help_text() -> None:
    from imp.cli.slash import _help
    lines: list[str] = []
    _help(lines.append, materialized=False)
    assert any("/abandon" in line for line in lines)


def test_keep_builtin_returns_literal_for_the_gate_loop(tmp_path: Path) -> None:
    # /keep is a built-in (NOT a skill): it returns the literal so the session gate
    # loop (cli/session_terminal) records the keep-as-draft intent and detaches.
    quest = SimpleNamespace(control_root=tmp_path)
    out: list[str] = []
    result = handle_slash_line("/keep", quest=quest, output_fn=out.append)
    assert result == "/keep"
    assert not any("no such skill" in line for line in out)


def test_keep_is_in_the_help_text() -> None:
    from imp.cli.slash import _help
    lines: list[str] = []
    _help(lines.append, materialized=False)
    assert any("/keep" in line for line in lines)
    assert not any("/save" in line for line in lines)


def test_clear_builtin_returns_literal_for_the_gate_loop(tmp_path: Path) -> None:
    # /clear is a built-in (NOT a skill): it returns the literal so the session
    # gate loop records the clear intent and detaches like Ctrl-D.
    quest = SimpleNamespace(control_root=tmp_path)
    out: list[str] = []
    result = handle_slash_line("/clear", quest=quest, output_fn=out.append)
    assert result == "/clear"
    assert not any("no such skill" in line for line in out)


def test_clear_is_in_the_help_text() -> None:
    from imp.cli.slash import _help
    lines: list[str] = []
    _help(lines.append, materialized=False)
    assert any("/clear" in line for line in lines)


def test_shelve_builtin_returns_literal_for_the_gate_loop(tmp_path: Path) -> None:
    # /shelve is a built-in (NOT a skill): it returns the literal so the session
    # gate loop records the shelve intent and detaches (archive, no reopen).
    quest = SimpleNamespace(control_root=tmp_path)
    out: list[str] = []
    result = handle_slash_line("/shelve", quest=quest, output_fn=out.append)
    assert result == "/shelve"
    assert not any("no such skill" in line for line in out)


def test_shelve_is_in_the_help_text() -> None:
    from imp.cli.slash import _help
    lines: list[str] = []
    _help(lines.append, materialized=False)
    assert any("/shelve" in line for line in lines)


def test_help_prelive_lists_leave_commands_and_leave_keybinding() -> None:
    from imp.cli.slash import _help
    lines: list[str] = []
    _help(lines.append, materialized=False)
    body = "\n".join(lines)
    assert "/keep" in body
    assert "/shelve" in body
    assert "/clear" in body
    assert "/abandon" in body
    assert "Leave this conversation; resume with imp resume <id>" in body
    assert "Pause and exit" not in body
    # /clear names "conversation", not "chat".
    assert "/clear             Shelve this conversation and start a fresh one" in body
    assert "chat" not in body


def test_help_materialized_omits_leave_commands_and_uses_pause_keybinding() -> None:
    from imp.cli.slash import _help
    lines: list[str] = []
    _help(lines.append, materialized=True)
    body = "\n".join(lines)
    # Command lines are absent (leading-space format distinguishes from "shelved"
    # in the /list description which contains "/shelved" as a substring).
    assert not any(line.strip().startswith("/keep") for line in lines)
    assert not any(line.strip().startswith("/shelve") for line in lines)
    assert not any(line.strip().startswith("/clear") for line in lines)
    assert "/abandon" in body                       # stays available live
    assert "Pause and exit; resume with imp resume <id>" in body
    assert "Leave this conversation" not in body


@pytest.mark.parametrize("command", ["/keep", "/shelve", "/clear"])
def test_leave_command_on_materialized_quest_is_rejected(
    tmp_path: Path, command: str,
) -> None:
    _seed_control(tmp_path)
    quest = Quest.create_draft(
        tmp_path, title="Live one", quest_id="mat9", base_branch="main")
    # Flip to materialized ON DISK; the in-memory `quest` stays stale (False),
    # so a pass requires the guard to reload — the freshness /abandon relies on.
    meta_path = quest.quest_dir / "quest.json"
    meta = json.loads(meta_path.read_text())
    meta["materialized"] = True
    meta_path.write_text(json.dumps(meta))
    out: list[str] = []
    result = handle_slash_line(command, quest=quest, output_fn=out.append)
    assert result is REPROMPT                        # no disposition literal returned
    expected = (
        "/keep, /shelve, and /clear apply only before a quest is built — this "
        "one is live. Press Ctrl-D to pause it (resume with imp resume mat9), "
        "or /abandon to tear it down.")
    assert any(expected in line for line in out)


@pytest.mark.parametrize("command", ["/keep", "/shelve", "/clear"])
def test_leave_command_on_prelive_quest_returns_literal(
    tmp_path: Path, command: str,
) -> None:
    _seed_control(tmp_path)
    quest = Quest.create_draft(
        tmp_path, title="Draft", quest_id="pre9", base_branch="main")
    out: list[str] = []
    result = handle_slash_line(command, quest=quest, output_fn=out.append)
    assert result == command                         # dispatched for the gate loop
    assert not any("apply only before a quest is built" in line for line in out)


def test_help_is_echoed_with_caret(tmp_path: Path) -> None:
    quest = SimpleNamespace()
    out: list[str] = []
    result = handle_slash_line("/help", quest=quest, output_fn=out.append, color=False)
    assert result is REPROMPT
    assert out[0] == "> /help"          # caret echo heads the command's own output


def test_valid_model_is_not_echoed_here(tmp_path: Path, monkeypatch) -> None:
    import imp.cli.slash as slash
    monkeypatch.setattr(slash, "validate_model_arg", lambda arg, *, quest: None)
    quest = SimpleNamespace()
    out: list[str] = []
    result = slash.handle_slash_line(
        "/model anthropic:claude-fable-5", quest=quest, output_fn=out.append,
        color=False)
    assert result == "/model anthropic:claude-fable-5"
    assert not any(line.startswith("> ") for line in out)


def test_invalid_model_is_echoed(tmp_path: Path, monkeypatch) -> None:
    import imp.cli.slash as slash
    monkeypatch.setattr(
        slash, "validate_model_arg",
        lambda arg, *, quest: "provider 'openai' is not available")
    quest = SimpleNamespace()
    out: list[str] = []
    result = slash.handle_slash_line(
        "/model openai:gpt-5", quest=quest, output_fn=out.append, color=False)
    assert result is slash.REPROMPT
    assert out[0] == "> /model openai:gpt-5"


def test_bare_model_roster_is_echoed(tmp_path: Path, monkeypatch) -> None:
    import imp.cli.slash as slash
    monkeypatch.setattr(
        slash, "print_model_roster",
        lambda *, quest, output_fn: output_fn("Current: x"))
    quest = SimpleNamespace()
    out: list[str] = []
    result = slash.handle_slash_line("/model", quest=quest, output_fn=out.append,
                                     color=False)
    assert result is slash.REPROMPT
    assert out[0] == "> /model"


def test_skill_and_pasted_path_are_not_command_echoed(tmp_path: Path) -> None:
    quest = SimpleNamespace(workflow_dir=tmp_path, control_root=tmp_path)
    out: list[str] = []
    result = handle_slash_line(
        "/home/user/foo.py", quest=quest, output_fn=out.append, color=False)
    assert result == "/home/user/foo.py"
    assert out == []


def test_skills_is_echoed_with_caret(tmp_path: Path) -> None:
    quest = SimpleNamespace(workflow_dir=tmp_path)   # empty skills dir -> "none" listing
    out: list[str] = []
    result = handle_slash_line("/skills", quest=quest, output_fn=out.append, color=False)
    assert result is REPROMPT
    assert out[0] == "> /skills"
