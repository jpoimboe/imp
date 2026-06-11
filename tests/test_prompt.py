"""Prompt abstraction over prompt_toolkit input layer."""

from __future__ import annotations

import pytest

from imp.cli.prompt import Prompt, _placeholder_text, _prompt_message_text


def test_prompt_returns_user_text() -> None:
    p = Prompt(_session_factory=lambda: _FakeSession(["hello"]))
    assert p.ask("> ") == "hello"


def test_ctrl_c_raises_keyboardinterrupt() -> None:
    p = Prompt(_session_factory=lambda: _FakeSession(raises=KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        p.ask("> ")


def test_ctrl_d_raises_eoferror() -> None:
    p = Prompt(_session_factory=lambda: _FakeSession(raises=EOFError))
    with pytest.raises(EOFError):
        p.ask("> ")


def test_history_persists_across_asks() -> None:
    """Ask twice; the second ask's session should see the first's input in
    its history. Verified by inspecting the history object."""
    p = Prompt(_session_factory=lambda: _FakeSession(["first", "second"]))
    p.ask("> ")
    p.ask("> ")
    assert "first" in p.history_strings()


def test_build_session_vi_mode() -> None:
    from prompt_toolkit.enums import EditingMode

    from imp.cli.prompt import _build_session
    session = _build_session(None, editing_mode="vi")
    assert session.editing_mode == EditingMode.VI


def test_build_session_applies_no_style_override() -> None:
    """The input region keeps the terminal default — user-turn identity now
    lives in the caret/rules, not a PromptSession bg fill. So no style override
    is set (the field never paints its own background again)."""
    from imp.cli.prompt import _build_session
    assert _build_session(None).style is None


def test_caret_is_bright_blue_under_color() -> None:
    """With color on, the prompt message is a prompt_toolkit ANSI caret carrying
    the bright-blue '>'; with color off it's the plain '> ' string."""
    from prompt_toolkit.formatted_text import ANSI

    on = Prompt(color=True)._message(None)
    assert isinstance(on, ANSI)
    assert "\x1b[94m" in on.value and ">" in on.value
    assert Prompt(color=False)._message(None) == "> "


class _FakeSession:
    def __init__(self, answers: list[str] | None = None, raises=None) -> None:
        self.answers = list(answers or [])
        self.raises = raises
        self._history: list[str] = []

    def prompt(self, _msg: str, *, handle_sigint: bool = True) -> str:
        # ask() now passes handle_sigint=False so an OS SIGINT routes through
        # imp's InterruptCoordinator, not a prompt_toolkit KeyboardInterrupt.
        assert handle_sigint is False
        if self.raises:
            raise self.raises
        answer = self.answers.pop(0)
        self._history.append(answer)
        return answer

    @property
    def history(self) -> _FakeHistory:
        return _FakeHistory(self._history)


class _FakeHistory:
    def __init__(self, strings: list[str]) -> None:
        self._strings = strings

    def get_strings(self) -> list[str]:
        return list(self._strings)


def test_message_is_bare_caret_without_a_question_plain() -> None:
    assert _prompt_message_text("", color=False) == "> "


def test_message_puts_the_question_above_the_caret_plain() -> None:
    assert _prompt_message_text("Pick A or B?", color=False) == "Pick A or B?\n> "


def test_message_strips_question_whitespace_plain() -> None:
    assert _prompt_message_text("  Pick A or B?  ", color=False) == "Pick A or B?\n> "


def test_message_color_dims_question_and_blues_caret() -> None:
    msg = _prompt_message_text("Pick A or B?", color=True)
    assert msg.startswith("\x1b[2mPick A or B?\x1b[0m\n")   # dim question line
    assert msg.endswith("\x1b[94m> \x1b[0m")                # bright-blue caret


def test_placeholder_hint_is_the_dim_reply_prompt() -> None:
    assert _placeholder_text(color=False) == "type your reply"
    assert _placeholder_text(color=True) == "\x1b[2mtype your reply\x1b[0m"
