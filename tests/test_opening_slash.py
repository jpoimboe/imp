from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import imp.cli.main as main


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _QueuePrompt:
    """Returns queued lines in order from ask_async (accepts the interceptor's kwargs)."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = list(lines)

    async def ask_async(self, prompt_text: str | None = None, **kwargs: object) -> str:
        return self._lines.pop(0)


@pytest.mark.anyio
async def test_opening_prompt_intercepts_help_then_returns_intent(
    capsys: pytest.CaptureFixture[str],
) -> None:
    change = SimpleNamespace(id="z9y8x7", title="")
    reply = await main._read_opening_message(
        change, leave_mode="new", banner_shown=False, color=False,
        width=80, editing_mode="emacs",
        _prompt_factory=lambda: _QueuePrompt(["/help", "add oauth"]))
    # /help was handled client-side and the loop kept reading; only the real
    # message becomes the intent (the old direct-read path returned "/help").
    assert reply == "add oauth"
    out = capsys.readouterr().out
    assert "Available slash commands" in out
    assert "Keybindings" in out


@pytest.mark.anyio
async def test_opening_prompt_session_verb_reprompts(
    capsys: pytest.CaptureFixture[str],
) -> None:
    change = SimpleNamespace(id="z9y8x7", title="")
    reply = await main._read_opening_message(
        change, leave_mode="new", banner_shown=False, color=False,
        width=80, editing_mode="emacs",
        _prompt_factory=lambda: _QueuePrompt(["/clear", "real intent"]))
    assert reply == "real intent"        # /clear did not become the draft intent
    out = capsys.readouterr().out
    assert "/clear" in out               # a note explained why it was ignored


@pytest.mark.anyio
async def test_opening_prompt_leading_slash_path_submits_as_input(
    tmp_path: Path,
) -> None:
    # A pasted absolute path is not a command — it must submit verbatim.
    change = SimpleNamespace(id="z9y8x7", title="", workflow_dir=tmp_path)
    reply = await main._read_opening_message(
        change, leave_mode="new", banner_shown=False, color=False,
        width=80, editing_mode="emacs",
        _prompt_factory=lambda: _QueuePrompt(["/home/user/foo.py"]))
    assert reply == "/home/user/foo.py"
