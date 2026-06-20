# tests/test_slash.py
from __future__ import annotations

import json
from pathlib import Path

import anyio
import pytest

from imp.cli.slash import REPROMPT, handle_slash_line, is_slash_command
from imp.cli.tui_app import HINT


class _FakeChange:
    """Minimal stand-in: handle_slash_line needs .workflow_dir and
    .event_log().append(...)."""

    def __init__(self, tmp_path: Path) -> None:
        self.workflow_dir = tmp_path / ".imp" / "change" / "workflow"
        skills = self.workflow_dir / "skills"
        skills.mkdir(parents=True)
        (skills / "refactor.md").write_text("# Refactor\nstep one\n")
        (skills / "debug.md").write_text("# Debug\n")
        self._events = tmp_path / "events.jsonl"

    def event_log(self):
        from imp.engine.state import JsonlEventLog
        return JsonlEventLog(self._events)


def test_is_slash_command() -> None:
    assert is_slash_command("/help")
    assert is_slash_command("  /help")  # leading whitespace tolerated
    assert not is_slash_command("hello")
    assert not is_slash_command("")


def test_name_loads_body_and_emits_event(tmp_path: Path) -> None:
    change = _FakeChange(tmp_path)
    out: list[str] = []
    result = handle_slash_line("/refactor", change=change, output_fn=out.append)
    assert result == "# Refactor\nstep one\n"
    events = [json.loads(line) for line in change._events.read_text().splitlines()]
    loads = [e for e in events if e["type"] == "skill_load"]
    assert loads[0]["payload"] == {"name": "refactor", "source": "human"}
    assert loads[0]["agent_id"] is None


def test_help_reprompts_and_prints(tmp_path: Path) -> None:
    change = _FakeChange(tmp_path)
    out: list[str] = []
    assert handle_slash_line("/help", change=change, output_fn=out.append) is REPROMPT
    assert any("/help" in line for line in out)


def test_skills_lists_names(tmp_path: Path) -> None:
    change = _FakeChange(tmp_path)
    out: list[str] = []
    assert handle_slash_line("/skills", change=change, output_fn=out.append) is REPROMPT
    joined = "\n".join(out)
    assert "refactor" in joined and "debug" in joined


def test_unknown_command_reprompts(tmp_path: Path) -> None:
    change = _FakeChange(tmp_path)
    out: list[str] = []
    assert handle_slash_line("/nope", change=change, output_fn=out.append) is REPROMPT
    assert any("nope" in line for line in out)


def test_skills_builtin_cannot_be_shadowed(tmp_path: Path) -> None:
    change = _FakeChange(tmp_path)
    (change.workflow_dir / "skills" / "skills.md").write_text("SHADOW\n")
    out: list[str] = []
    # /skills resolves to the builtin listing, never the skill body.
    result = handle_slash_line("/skills", change=change, output_fn=out.append)
    assert result is REPROMPT
    assert "SHADOW" not in "\n".join(out)


class _FakePrompt:
    """Returns queued lines in order from ask_async; records the question kwarg."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = list(lines)
        self.questions: list[str] = []

    async def ask_async(self, prompt_text: str | None = None, *,
                        question: str = "", width: int = 80, indent: str = "") -> str:
        self.questions.append(question)
        return self._lines.pop(0)


class _NullStatus:
    def suspend(self):
        import contextlib
        return contextlib.nullcontext()


def test_bare_slash_reprompts_with_help(tmp_path: Path) -> None:
    change = _FakeChange(tmp_path)
    out: list[str] = []
    assert handle_slash_line("/", change=change, output_fn=out.append) is REPROMPT
    assert any("/help" in line for line in out)


def test_ask_line_with_slash_passes_through_plain_text(tmp_path: Path) -> None:
    from imp.cli.slash import ask_line_with_slash
    change = _FakeChange(tmp_path)
    prompt = _FakePrompt(["just a message"])
    line = anyio.run(ask_line_with_slash, prompt, _NullStatus(), change)
    assert line == "just a message"


def test_ask_line_with_slash_loops_on_help_then_returns(tmp_path: Path) -> None:
    from imp.cli.slash import ask_line_with_slash
    change = _FakeChange(tmp_path)
    prompt = _FakePrompt(["/help", "real input"])
    line = anyio.run(ask_line_with_slash, prompt, _NullStatus(), change)
    assert line == "real input"


def test_ask_line_with_slash_returns_skill_body(tmp_path: Path) -> None:
    from imp.cli.slash import ask_line_with_slash
    change = _FakeChange(tmp_path)
    prompt = _FakePrompt(["/refactor"])
    line = anyio.run(ask_line_with_slash, prompt, _NullStatus(), change)
    assert line == "# Refactor\nstep one\n"


def test_skill_args_appended_after_body(tmp_path: Path) -> None:
    # `/refactor src/foo.py` loads skill `refactor` and appends the argument
    # text after the body, separated by a blank line.
    change = _FakeChange(tmp_path)
    out: list[str] = []
    result = handle_slash_line(
        "/refactor src/foo.py", change=change, output_fn=out.append
    )
    assert result == "# Refactor\nstep one\n\nsrc/foo.py\n"


def test_skill_args_preserve_internal_spacing_strip_edges(tmp_path: Path) -> None:
    change = _FakeChange(tmp_path)
    out: list[str] = []
    result = handle_slash_line(
        "/refactor   keep  this   ", change=change, output_fn=out.append
    )
    assert result == "# Refactor\nstep one\n\nkeep  this\n"


def test_skill_without_args_returns_body_unchanged(tmp_path: Path) -> None:
    change = _FakeChange(tmp_path)
    out: list[str] = []
    result = handle_slash_line("/refactor", change=change, output_fn=out.append)
    assert result == "# Refactor\nstep one\n"


def test_skill_args_event_still_records_name_only(tmp_path: Path) -> None:
    # The attribution event stays {name, source}; the argument rides the
    # returned user message, not the event payload.
    change = _FakeChange(tmp_path)
    handle_slash_line("/refactor src/foo.py", change=change, output_fn=[].append)
    events = [json.loads(line) for line in change._events.read_text().splitlines()]
    loads = [e for e in events if e["type"] == "skill_load"]
    assert loads[0]["payload"] == {"name": "refactor", "source": "human"}


def test_exit_and_quit_words_request_save_and_exit(tmp_path: Path) -> None:
    # `exit` / `quit` typed at the prompt are the save-and-exit verbs (equivalent
    # to Ctrl-D), surfaced as EOFError to the run loop's existing EOFError handler.
    from imp.cli.slash import ask_line_with_slash

    for i, word in enumerate(("exit", "quit", "  EXIT  ")):   # case/space-insensitive
        change = _FakeChange(tmp_path / str(i))
        prompt = _FakePrompt([word])
        with pytest.raises(EOFError):
            anyio.run(ask_line_with_slash, prompt, _NullStatus(), change)


def test_no_chrome_printed_around_the_submitted_prompt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """ask_line_with_slash prints NO chrome around the field: no rule, no blank
    line. The bright-blue caret marks the turn while typing; on submit
    prompt_toolkit erases its region and the live driver reprints the entered line
    with the subtle background. So a plain submit produces no stdout here."""
    from imp.cli.slash import ask_line_with_slash
    prompt = _FakePrompt(["hello"])
    line = anyio.run(ask_line_with_slash, prompt, _NullStatus(), _FakeChange(tmp_path))
    assert line == "hello"
    assert capsys.readouterr().out == ""


class _RecDriver:
    """Recording driver stub: tracks prepare_for_prompt / flush_deferred calls."""

    def __init__(self) -> None:
        self.events: list[str] = []

    def prepare_for_prompt(self) -> None:
        self.events.append("prepare")

    def flush_deferred(self) -> None:
        self.events.append("flush")

    def prompt_indent(self) -> str:
        return ""


class _CountStatus:
    """Status stub that counts how many times suspend() is entered."""

    def __init__(self) -> None:
        self.suspend_calls = 0

    def suspend(self) -> object:
        import contextlib as _cm
        self.suspend_calls += 1
        return _cm.nullcontext()


def test_solicit_prepares_then_flushes_once(tmp_path: Path) -> None:
    """driver.prepare_for_prompt fires before the prompt; flush_deferred fires
    exactly once (in the finally) on normal exit.  status.suspend() is called
    exactly once (hoisted above the loop, not per-iteration)."""
    from imp.cli.slash import ask_line_with_slash
    drv = _RecDriver()
    status = _CountStatus()
    change = _FakeChange(tmp_path)
    prompt = _FakePrompt(["just a line"])

    async def _run() -> str:
        return await ask_line_with_slash(prompt, status, change, driver=drv)

    line = anyio.run(_run)
    assert line == "just a line"
    assert drv.events == ["prepare", "flush"]
    assert status.suspend_calls == 1   # exactly one hoisted suspend, not per-iteration


def test_solicit_flushes_on_slash_reprompt(tmp_path: Path) -> None:
    """flush_deferred fires once even when a slash command reprompts first."""
    from imp.cli.slash import ask_line_with_slash
    drv = _RecDriver()
    status = _CountStatus()
    change = _FakeChange(tmp_path)
    # /help reprompts, then "real input" returns
    prompt = _FakePrompt(["/help", "real input"])

    async def _run() -> str:
        return await ask_line_with_slash(prompt, status, change, driver=drv)

    line = anyio.run(_run)
    assert line == "real input"
    # prepare fires twice (once per loop iteration), flush fires once (in finally)
    assert drv.events == ["prepare", "prepare", "flush"]
    assert status.suspend_calls == 1   # hoisted: still just one suspend


def test_hint_and_help_agree_on_esc_ctrlc_vocabulary(tmp_path: Path) -> None:
    change = _FakeChange(tmp_path)
    out: list[str] = []
    handle_slash_line("/help", change=change, output_fn=out.append)
    ctrl_c_line = next(line for line in out if "Ctrl+C" in line)

    # Both surfaces describe ESC/Ctrl-C with "pause", neither with "interrupt".
    assert "pause" in ctrl_c_line.lower()
    assert "Esc" in ctrl_c_line          # Esc is explicitly listed in /help
    assert "interrupt" not in ctrl_c_line.lower()
    assert "pause" in HINT.lower()
    assert "interrupt" not in HINT.lower()
