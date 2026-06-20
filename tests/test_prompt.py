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
    """With color off (the default), the input region keeps the terminal default —
    no PromptSession bg fill. The live slate is opt-in: it paints only under
    color + a truecolor theme (see the two tests below)."""
    from imp.cli.prompt import _build_session
    assert _build_session(None).style is None


def test_build_session_paints_slate_field_under_truecolor() -> None:
    from prompt_toolkit.output import ColorDepth

    from imp.cli import palette as pal
    from imp.cli.prompt import _build_session

    pal.set_theme("dark")
    try:
        s = _build_session(None, color=True)
    finally:
        pal.set_theme("ansi")
    assert s.style.style_rules == [("", "bg:#2b2f3a")]   # default selector fills line
    assert s.color_depth == ColorDepth.TRUE_COLOR


def test_build_session_no_slate_under_ansi_theme() -> None:
    from imp.cli import palette as pal
    from imp.cli.prompt import _build_session

    pal.set_theme("ansi")
    s = _build_session(None, color=True)
    assert s.style is None and s.color_depth is None


def test_default_caret_is_the_blue_arrow() -> None:
    """The default caret is the `> ` arrow: bright-blue under color, plain `> `
    otherwise. _message(None) emits it; an explicit prompt_text is still styled."""
    from prompt_toolkit.formatted_text import ANSI

    on = Prompt(color=True)._message(None)
    assert isinstance(on, ANSI)
    assert "\x1b[94m" in on.value and "> " in on.value
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


def test_message_renders_markdown_question_above_the_caret() -> None:
    msg = _prompt_message_text("Reply **merge** to merge.", color=True, width=80)
    assert "\x1b[1m" in msg                          # bold for **merge**
    assert "**" not in msg                           # asterisks consumed
    assert msg.endswith("\x1b[94m> \x1b[0m")          # styled `> ` caret line last


def test_message_no_color_renders_markdown_without_ansi() -> None:
    msg = _prompt_message_text("Reply **merge** to merge.", color=False, width=80)
    assert "**" not in msg      # asterisks consumed by markdown
    assert "\x1b[" not in msg   # no ANSI codes on the no-color path
    assert "merge" in msg       # content present
    assert msg.endswith("> ")                          # plain `> ` caret line last


def test_message_plain_question_still_sits_above_the_caret() -> None:
    assert _prompt_message_text("Pick A or B?", color=False, width=80) \
        == "Pick A or B?\n> "


def test_placeholder_hint_is_the_dim_reply_prompt() -> None:
    assert _placeholder_text(color=False) == "type your reply"
    assert _placeholder_text(color=True) == "\x1b[2mtype your reply\x1b[0m"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_ask_async_caret_prefix_with_indent() -> None:
    captured: dict[str, object] = {}

    class _SpySession:
        async def prompt_async(self, msg, **kwargs):  # type: ignore[no-untyped-def]
            captured["msg"] = msg
            captured.update(kwargs)
            return "ok"

    prompt = Prompt(color=False, _session_factory=lambda: _SpySession())
    line = await prompt.ask_async(indent="  ")
    assert line == "ok"
    # indent (2) + `> ` caret -> body text at column 4.
    assert captured["msg"] == "  > "
    cont = captured["prompt_continuation"]
    # Pad = len(indent)=2 + len(CARET)=2 -> 4 cols, under the first line's body.
    assert cont(80, 1, False) == "    "


@pytest.mark.anyio
async def test_ask_async_caret_prefix_at_root() -> None:
    captured: dict[str, object] = {}

    class _SpySession:
        async def prompt_async(self, msg, **kwargs):  # type: ignore[no-untyped-def]
            captured["msg"] = msg
            captured.update(kwargs)
            return ""

    prompt = Prompt(color=False, _session_factory=lambda: _SpySession())
    await prompt.ask_async()
    assert captured["msg"] == "> "                        # `> ` caret, body at col 2
    assert captured["prompt_continuation"](80, 1, False) == "  "
    # The plain hint must still render (not None) even with color off.
    assert captured["placeholder"] == "type your reply"


@pytest.mark.anyio
async def test_ask_async_question_path_caret_at_content_column() -> None:
    captured: dict[str, object] = {}

    class _SpySession:
        async def prompt_async(self, msg, **kwargs):  # type: ignore[no-untyped-def]
            captured["msg"] = msg
            captured.update(kwargs)
            return "yes"

    prompt = Prompt(color=False, _session_factory=lambda: _SpySession())
    await prompt.ask_async(question="Merge it?", indent="  ")
    # The question block and the `> ` caret line are both prefixed with the indent.
    assert captured["msg"] == "  Merge it?\n  > "
    assert captured["prompt_continuation"](80, 1, False) == "    "
