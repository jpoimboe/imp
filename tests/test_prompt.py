"""Prompt abstraction over prompt_toolkit input layer."""

from __future__ import annotations

import pytest

from imp.cli.prompt import Prompt


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


def test_build_session_color_true_paints_input_bg() -> None:
    """When color is enabled, `_build_session` passes a prompt_toolkit Style
    that paints the default class (the input region) with bright-blue bg —
    matching the `bg_bright_blue` ANSI code used by imp log for
    UserMessageNode. Both single- and multi-line input pick this style up
    because PromptSession applies its `style=` to the whole input field."""
    from imp.cli.prompt import _build_session
    sess = _build_session(None, color=True)
    assert sess.style is not None
    rules = sess.style.style_rules
    assert ("", "bg:ansibrightblue") in rules


def test_build_session_color_false_omits_style() -> None:
    """When color is disabled (NO_COLOR / non-TTY), no Style is passed and the
    PromptSession falls back to the default (no bg)."""
    from imp.cli.prompt import _build_session
    sess = _build_session(None, color=False)
    assert sess.style is None


def test_prompt_threads_color_into_session_factory() -> None:
    """The public Prompt(...) constructor accepts `color` and forwards it to
    the default `_session_factory`."""
    from imp.cli.prompt import Prompt
    p_on = Prompt(color=True)
    p_off = Prompt(color=False)
    assert p_on._session.style is not None
    assert p_off._session.style is None


class _FakeSession:
    def __init__(self, answers: list[str] | None = None, raises=None) -> None:
        self.answers = list(answers or [])
        self.raises = raises
        self._history: list[str] = []

    def prompt(self, _msg: str) -> str:
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
