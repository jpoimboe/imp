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


def test_build_session_has_no_app_background_and_reserves_no_menu_space() -> None:
    """AC1: the prompt region keeps the terminal default (no app bg fill) and
    prompt_toolkit holds no blank rows under the prompt — even under color + a
    truecolor theme, where the old slate fill used to paint."""
    from imp.cli import palette as pal
    from imp.cli.prompt import _build_session

    assert _build_session(None).style is None
    assert _build_session(None).reserve_space_for_menu == 0

    pal.set_theme("dark")
    try:
        s = _build_session(None, color=True)
    finally:
        pal.set_theme("ansi")
    assert s.style is None
    assert s.reserve_space_for_menu == 0


def test_default_caret_is_the_gold_arrow() -> None:
    """The default caret is the `> ` arrow: gold (wordmark) under color, plain `> `
    otherwise. _message(None) emits it; an explicit prompt_text is still styled."""
    from prompt_toolkit.formatted_text import ANSI

    on = Prompt(color=True)._message(None)
    assert isinstance(on, ANSI)
    assert "\x1b[33m" in on.value and "> " in on.value
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
    assert _prompt_message_text("Pick A or B?", color=False) == "Pick A or B?\n\n> "


def test_message_strips_question_whitespace_plain() -> None:
    assert _prompt_message_text("  Pick A or B?  ", color=False) == "Pick A or B?\n\n> "


def test_message_renders_markdown_question_above_the_caret() -> None:
    msg = _prompt_message_text("Reply **merge** to merge.", color=True, width=80)
    assert "\x1b[1m" in msg                          # bold for **merge**
    assert "**" not in msg                           # asterisks consumed
    assert msg.endswith("\x1b[33m> \x1b[0m")          # styled `> ` caret line last


def test_message_no_color_renders_markdown_without_ansi() -> None:
    msg = _prompt_message_text("Reply **merge** to merge.", color=False, width=80)
    assert "**" not in msg      # asterisks consumed by markdown
    assert "\x1b[" not in msg   # no ANSI codes on the no-color path
    assert "merge" in msg       # content present
    assert msg.endswith("> ")                          # plain `> ` caret line last


def test_message_plain_question_still_sits_above_the_caret() -> None:
    assert _prompt_message_text("Pick A or B?", color=False, width=80) \
        == "Pick A or B?\n\n> "


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


def test_placeholder_text_accepts_custom_text() -> None:
    assert _placeholder_text(color=False, text="type a message, or /help for commands") \
        == "type a message, or /help for commands"
    assert _placeholder_text(color=True, text="type a message, or /help for commands") \
        == "\x1b[2mtype a message, or /help for commands\x1b[0m"


@pytest.mark.anyio
async def test_ask_async_opening_hint_overrides_placeholder() -> None:
    from prompt_toolkit.formatted_text import ANSI

    captured: dict[str, object] = {}

    class _SpySession:
        async def prompt_async(self, msg, **kwargs):  # type: ignore[no-untyped-def]
            captured.update(kwargs)
            return ""

    prompt = Prompt(color=False, _session_factory=lambda: _SpySession())
    await prompt.ask_async(question="What's on your mind?",
                           hint="type a message, or /help for commands")
    assert captured["placeholder"] == "type a message, or /help for commands"

    captured_color: dict[str, object] = {}

    class _SpySessionColor:
        async def prompt_async(self, msg, **kwargs):  # type: ignore[no-untyped-def]
            captured_color.update(kwargs)
            return ""

    prompt_color = Prompt(color=True, _session_factory=lambda: _SpySessionColor())
    await prompt_color.ask_async(question="What's on your mind?",
                                 hint="type a message, or /help for commands")
    ph = captured_color["placeholder"]
    assert isinstance(ph, ANSI)
    assert ph.value == "\x1b[2mtype a message, or /help for commands\x1b[0m"


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
    assert captured["msg"] == "  Merge it?\n\n  > "
    assert captured["prompt_continuation"](80, 1, False) == "    "


@pytest.mark.anyio
async def test_ask_async_passes_plain_bottom_toolbar_through() -> None:
    captured: dict[str, object] = {}

    class _SpySession:
        async def prompt_async(self, msg, **kwargs):  # type: ignore[no-untyped-def]
            captured.update(kwargs)
            return "ok"

    prompt = Prompt(color=False, _session_factory=lambda: _SpySession())
    line = await prompt.ask_async(bottom_toolbar="z9gixx · 13%")
    assert line == "ok"
    assert captured["bottom_toolbar"] == "\nz9gixx · 13%"  # blank line separates prompt from bar
    from prompt_toolkit.styles import Style
    assert "style" in captured
    assert isinstance(captured["style"], Style)


@pytest.mark.anyio
async def test_ask_async_wraps_colored_bottom_toolbar_as_ansi_with_style() -> None:
    from prompt_toolkit.formatted_text import ANSI
    captured: dict[str, object] = {}

    class _SpySession:
        async def prompt_async(self, msg, **kwargs):  # type: ignore[no-untyped-def]
            captured.update(kwargs)
            return "ok"

    prompt = Prompt(color=True, _session_factory=lambda: _SpySession())
    await prompt.ask_async(bottom_toolbar="\x1b[35mmodel\x1b[39m")
    assert isinstance(captured["bottom_toolbar"], ANSI)   # parsed so the SGR survives
    # the spaced value (with its leading newline) is wrapped, not the original
    assert captured["bottom_toolbar"].value == "\n\x1b[35mmodel\x1b[39m"
    assert "style" in captured                            # neutralizes default reverse


@pytest.mark.anyio
async def test_ask_async_forwards_empty_string_toolbar() -> None:
    captured: dict[str, object] = {}

    class _SpySession:
        async def prompt_async(self, msg, **kwargs):  # type: ignore[no-untyped-def]
            captured.update(kwargs)
            return "ok"

    prompt = Prompt(color=False, _session_factory=lambda: _SpySession())
    await prompt.ask_async(bottom_toolbar="")             # falsy but not None -> still forwards
    assert captured["bottom_toolbar"] == ""               # an `is not None` gate, not truthiness


@pytest.mark.anyio
async def test_ask_async_without_toolbar_passes_no_toolbar_kwarg() -> None:
    captured: dict[str, object] = {}

    class _SpySession:
        async def prompt_async(self, msg, **kwargs):  # type: ignore[no-untyped-def]
            captured.update(kwargs)
            return "ok"

    prompt = Prompt(color=False, _session_factory=lambda: _SpySession())
    await prompt.ask_async()                               # default: no toolbar
    assert "bottom_toolbar" not in captured               # pre-toolbar callers unchanged
