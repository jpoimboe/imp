# tests/test_slash.py
from __future__ import annotations

import json
from pathlib import Path

import anyio

from imp.cli.prompt import ESC_SENTINEL
from imp.cli.slash import REPROMPT, handle_slash_line, is_slash_command


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
    assert not is_slash_command(ESC_SENTINEL)  # ESC sentinel ("__IMP_ESC__") passes through


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
    """Returns queued lines in order from ask_async."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = list(lines)

    async def ask_async(self, prompt_text: str = "> ") -> str:
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


def test_ask_line_with_slash_passes_through_esc(tmp_path: Path) -> None:
    from imp.cli.prompt import ESC_SENTINEL
    from imp.cli.slash import ask_line_with_slash
    change = _FakeChange(tmp_path)
    prompt = _FakePrompt([ESC_SENTINEL])
    line = anyio.run(ask_line_with_slash, prompt, _NullStatus(), change)
    assert line == ESC_SENTINEL
