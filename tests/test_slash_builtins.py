from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from imp.cli.slash import REPROMPT, handle_slash_line
from imp.engine.quest import Quest
from tests.conftest import _seed_control

HELP_PAUSE = "  /pause             Pause and exit; resume with imp resume <id>"
HELP_ARCHIVE = "  /archive           Archive this conversation (find it later with imp search)"
HELP_CLEAR = "  /clear             Archive this conversation and start a fresh one"
CTRL_D_LEAVE = "  Ctrl+D             Leave this conversation; resume with imp resume <id>"
CTRL_D_PAUSE = "  Ctrl+D             Pause and exit; resume with imp resume <id>"


def test_ls_is_unknown_after_rename(tmp_path: Path) -> None:
    # `/ls` matches no builtin and no skill; command-shaped, so it is a real
    # "Unknown command" error rather than being submitted verbatim.
    quest = SimpleNamespace(control_root=tmp_path, workflow_dir=tmp_path)
    out: list[str] = []
    result = handle_slash_line("/ls", quest=quest, output_fn=out.append)
    assert result is REPROMPT
    assert any("Unknown command: /ls — /help for the list" in line for line in out)


def test_removed_builtins_error_as_unknown_command(tmp_path: Path) -> None:
    # /list, /search, /resume are no longer builtins: each matches no skill and,
    # being command-shaped, is a real "Unknown command" error.
    quest = SimpleNamespace(workflow_dir=tmp_path, control_root=tmp_path)
    for line, cmd in (("/list", "list"), ("/search dark", "search"),
                      ("/resume conv1", "resume")):
        out: list[str] = []
        assert handle_slash_line(line, quest=quest, output_fn=out.append) is REPROMPT
        assert any(f"Unknown command: /{cmd} — /help for the list" in o for o in out)


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


def test_pause_builtin_returns_literal_for_the_gate_loop(tmp_path: Path) -> None:
    # /pause is a built-in (NOT a skill): it returns the literal so the session gate
    # loop (cli/session_terminal) records the pause intent and detaches.
    quest = SimpleNamespace(control_root=tmp_path)
    out: list[str] = []
    result = handle_slash_line("/pause", quest=quest, output_fn=out.append)
    assert result == "/pause"
    assert out == []


def test_pause_is_in_the_help_text() -> None:
    from imp.cli.slash import _help
    lines: list[str] = []
    _help(lines.append, materialized=False)
    assert HELP_PAUSE in lines
    assert not any(line.strip().startswith(("/save", "/keep")) for line in lines)


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


def test_archive_builtin_returns_literal_for_the_gate_loop(tmp_path: Path) -> None:
    # /archive is a built-in (NOT a skill): it returns the literal so the session
    # gate loop records the archive intent and detaches (archive, no reopen).
    quest = SimpleNamespace(control_root=tmp_path)
    out: list[str] = []
    result = handle_slash_line("/archive", quest=quest, output_fn=out.append)
    assert result == "/archive"
    assert out == []


def test_archive_is_in_the_help_text() -> None:
    from imp.cli.slash import _help
    lines: list[str] = []
    _help(lines.append, materialized=False)
    assert HELP_ARCHIVE in lines
    assert not any(line.strip().startswith("/shelve") for line in lines)


@pytest.mark.parametrize("command", ["/save", "/shelve"])
def test_retired_leave_commands_are_unknown(tmp_path: Path, command: str) -> None:
    quest = SimpleNamespace(workflow_dir=tmp_path, control_root=tmp_path)
    out: list[str] = []
    assert handle_slash_line(command, quest=quest, output_fn=out.append) is REPROMPT
    assert f"Unknown command: {command} — /help for the list" in out


def test_help_prelive_lists_leave_commands_and_leave_keybinding() -> None:
    from imp.cli.slash import _help
    lines: list[str] = []
    _help(lines.append, materialized=False)
    assert HELP_PAUSE in lines
    assert HELP_ARCHIVE in lines
    assert HELP_CLEAR in lines
    assert any(line.strip().startswith("/abandon") for line in lines)
    assert [ln for ln in lines if ln.strip().startswith("Ctrl+D")] == [CTRL_D_LEAVE]
    # /clear names "conversation", not "chat".
    assert "chat" not in "\n".join(lines)


def test_help_materialized_lists_pause_and_omits_archive_and_clear() -> None:
    from imp.cli.slash import _help
    lines: list[str] = []
    _help(lines.append, materialized=True)
    assert HELP_PAUSE in lines
    assert not any(line.strip().startswith(("/archive", "/clear", "/save", "/shelve"))
                   for line in lines)
    assert any(line.strip().startswith("/abandon") for line in lines)   # stays available live
    assert [ln for ln in lines if ln.strip().startswith("Ctrl+D")] == [CTRL_D_PAUSE]
    assert "Leave this conversation" not in "\n".join(lines)


@pytest.mark.parametrize("command", ["/archive", "/clear"])
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
        "/archive and /clear apply only before a quest is built — this one is "
        "live. Type /pause or press Ctrl-D to pause it (resume with imp resume "
        "mat9), or /abandon to tear it down.")
    assert expected in out


def test_pause_on_materialized_quest_returns_literal(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    quest = Quest.create_draft(
        tmp_path, title="Live one", quest_id="mat8", base_branch="main")
    meta_path = quest.quest_dir / "quest.json"
    meta = json.loads(meta_path.read_text())
    meta["materialized"] = True
    meta_path.write_text(json.dumps(meta))
    out: list[str] = []
    assert handle_slash_line("/pause", quest=quest, output_fn=out.append) == "/pause"
    assert out == []


@pytest.mark.timeout(240)  # ~9 git subprocesses starve under -n auto saturation
@pytest.mark.parametrize("command", ["/pause", "/archive", "/clear"])
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


PRE_SESSION_REDIRECT = (
    "/pause, /archive, and /clear apply once the conversation is running — "
    "send a message first.")


@pytest.mark.parametrize("command", ["/pause", "/archive", "/clear"])
def test_leave_command_pre_session_shows_redirect(
    tmp_path: Path, command: str,
) -> None:
    # Pre-session: no daemon, no conversation. The trio must not return a
    # disposition literal; it prints the pre-session redirect and re-prompts.
    quest = SimpleNamespace(id="pre9", control_root=tmp_path, workflow_dir=tmp_path)
    out: list[str] = []
    result = handle_slash_line(
        command, quest=quest, output_fn=out.append, pre_session=True)
    assert result is REPROMPT
    assert PRE_SESSION_REDIRECT in out
    assert out[0] == f"> {command}"          # command is echoed with the caret


@pytest.mark.parametrize("command", ["/pause", "/archive", "/clear"])
def test_leave_command_pre_session_takes_precedence_over_materialized(
    tmp_path: Path, command: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # pre_session wins even if the quest reads materialized: the pre-session copy
    # is shown, NOT the materialized redirect, and no disk reload is needed.
    monkeypatch.setattr("imp.cli.slash._is_materialized", lambda quest: True)
    quest = SimpleNamespace(id="pre9", control_root=tmp_path, workflow_dir=tmp_path)
    out: list[str] = []
    result = handle_slash_line(
        command, quest=quest, output_fn=out.append, pre_session=True)
    assert result is REPROMPT
    assert not any("apply only before a quest is built" in line for line in out)
    assert PRE_SESSION_REDIRECT in out


def test_abandon_pre_session_returns_literal(tmp_path: Path) -> None:
    # /abandon stays a pure dispatch even pre-session: the reader runs teardown.
    quest = SimpleNamespace(id="pre9", control_root=tmp_path, workflow_dir=tmp_path)
    out: list[str] = []
    result = handle_slash_line(
        "/abandon", quest=quest, output_fn=out.append, pre_session=True)
    assert result == "/abandon"


@pytest.mark.parametrize("unsandboxed", [False, True])
def test_sandbox_pre_session_off_echoes_state_off_and_returns_literal(
    tmp_path: Path, unsandboxed: bool,
) -> None:
    from imp.cli.slash import SANDBOX_STATE_OFF, handle_slash_line
    quest = SimpleNamespace(id="pre9", control_root=tmp_path, workflow_dir=tmp_path)
    out: list[str] = []
    result = handle_slash_line(
        "/sandbox off", quest=quest, output_fn=out.append,
        pre_session=True, unsandboxed=unsandboxed)
    assert result == "/sandbox off"
    assert out[0] == "> /sandbox off"
    assert any(SANDBOX_STATE_OFF in line for line in out)


def test_sandbox_pre_session_on_cancels_pending_off_returns_literal(
    tmp_path: Path,
) -> None:
    from imp.cli.slash import SANDBOX_STATE_ON, handle_slash_line
    quest = SimpleNamespace(id="pre9", control_root=tmp_path, workflow_dir=tmp_path)
    out: list[str] = []
    result = handle_slash_line(
        "/sandbox on", quest=quest, output_fn=out.append,
        pre_session=True, unsandboxed=True)
    assert result == "/sandbox on"
    assert any(SANDBOX_STATE_ON in line for line in out)


def test_sandbox_pre_session_on_with_nothing_pending_reprompts(
    tmp_path: Path,
) -> None:
    from imp.cli.slash import SANDBOX_ALREADY_ON, handle_slash_line
    quest = SimpleNamespace(id="pre9", control_root=tmp_path, workflow_dir=tmp_path)
    out: list[str] = []
    result = handle_slash_line(
        "/sandbox on", quest=quest, output_fn=out.append,
        pre_session=True, unsandboxed=False)
    assert result is REPROMPT
    assert any(SANDBOX_ALREADY_ON in line for line in out)


@pytest.mark.parametrize("pending,constant", [
    (True, "SANDBOX_STATE_OFF"),
    (False, "SANDBOX_STATE_ON"),
])
def test_sandbox_pre_session_no_arg_reports_pending_state(
    tmp_path: Path, pending: bool, constant: str,
) -> None:
    from imp.cli import slash
    quest = SimpleNamespace(id="pre9", control_root=tmp_path, workflow_dir=tmp_path)
    out: list[str] = []
    result = slash.handle_slash_line(
        "/sandbox", quest=quest, output_fn=out.append,
        pre_session=True, unsandboxed=pending)
    assert result is slash.REPROMPT
    assert any(getattr(slash, constant) in line for line in out)


def test_sandbox_pre_session_bad_arg_prints_usage(tmp_path: Path) -> None:
    from imp.cli.slash import SANDBOX_USAGE, handle_slash_line
    quest = SimpleNamespace(id="pre9", control_root=tmp_path, workflow_dir=tmp_path)
    out: list[str] = []
    result = handle_slash_line(
        "/sandbox maybe", quest=quest, output_fn=out.append, pre_session=True)
    assert result is REPROMPT
    assert any(SANDBOX_USAGE in line for line in out)


def test_sandbox_pre_session_constant_removed() -> None:
    from imp.cli import slash
    assert not hasattr(slash, "SANDBOX_PRE_SESSION")


@pytest.mark.parametrize("command,expected", [
    ("/sandbox on", "/sandbox on"),
    ("/sandbox off", "/sandbox off"),
])
def test_sandbox_mid_session_accept_paths_unchanged(
    tmp_path: Path, command: str, expected: str,
) -> None:
    # With pre_session False the toggle accept paths still return the literal
    # command (unsandboxed matched to the requested direction), unaffected by the
    # new pre-session branch.
    quest = SimpleNamespace(id="c1", control_root=tmp_path, workflow_dir=tmp_path)
    out: list[str] = []
    result = handle_slash_line(
        command, quest=quest, output_fn=out.append,
        unsandboxed=(command == "/sandbox on"))
    assert result == expected
    assert out == []


def test_compact_builtin_returns_literal_for_the_gate_loop(tmp_path: Path) -> None:
    # /compact is a built-in (NOT a skill): it returns the literal so the session
    # gate loop sends CONTROL_COMPACT; it carries no args, like /abandon.
    # Seeding a compact skill ensures the builtin arm wins over skill-load — the
    # test fails if the /compact arm is removed from handle_slash_line.
    skills_user = tmp_path / "skills" / "user"
    skills_user.mkdir(parents=True)
    (skills_user / "compact.md").write_text("x")
    quest = SimpleNamespace(control_root=tmp_path, workflow_dir=tmp_path)
    out: list[str] = []
    result = handle_slash_line("/compact", quest=quest, output_fn=out.append)
    assert result == "/compact"
    assert not any("no such skill" in line for line in out)
    assert out == []


def test_compact_is_in_the_help_text() -> None:
    from imp.cli.slash import _help
    lines: list[str] = []
    _help(lines.append, materialized=False)
    assert any("/compact" in line for line in lines)


def test_unknown_command_errors_after_compact(tmp_path: Path) -> None:
    # AC7d: adding /compact must not shadow the unknown-command handling.
    quest = SimpleNamespace(workflow_dir=tmp_path, control_root=tmp_path)
    out: list[str] = []
    result = handle_slash_line("/nope", quest=quest, output_fn=out.append)
    assert result is REPROMPT
    assert any("Unknown command: /nope — /help for the list" in line for line in out)


def test_help_pre_session_omits_leave_trio_keeps_abandon() -> None:
    from imp.cli.slash import _help
    lines: list[str] = []
    _help(lines.append, materialized=False, pre_session=True)
    body = "\n".join(lines)
    assert not any(line.strip().startswith(("/pause", "/archive", "/clear"))
                   for line in lines)
    assert "/abandon" in body
    # Keybindings block is unchanged: materialized=False still says "Leave...".
    assert "Leave this conversation; resume with imp resume <id>" in body
    assert "Pause and exit" not in body


COMPACT_PRE_SESSION_REDIRECT = (
    "/compact applies once the conversation is running — send a message first.")


def test_compact_pre_session_shows_redirect_and_reprompts(tmp_path: Path) -> None:
    quest = SimpleNamespace(id="pre9", control_root=tmp_path, workflow_dir=tmp_path)
    out: list[str] = []
    result = handle_slash_line(
        "/compact", quest=quest, output_fn=out.append, pre_session=True)
    assert result is REPROMPT
    assert out[0] == "> /compact"
    assert COMPACT_PRE_SESSION_REDIRECT in out


def test_help_pre_session_omits_compact() -> None:
    from imp.cli.slash import _help
    lines: list[str] = []
    _help(lines.append, materialized=False, pre_session=True)
    assert not any(line.strip().startswith("/compact") for line in lines)


def test_help_mid_session_lists_compact() -> None:
    from imp.cli.slash import _help
    lines: list[str] = []
    _help(lines.append, materialized=False, pre_session=False)
    assert any(line.strip().startswith("/compact") for line in lines)
