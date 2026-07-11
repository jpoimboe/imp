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


def test_build_session_appends_high_weight_filler_after_toolbar() -> None:
    """AC(a)/(b): the layout ends with a high-weight filler Window placed
    immediately after the bottom-toolbar container, so the renderer's forced
    full-height fill lands in the filler and the toolbar no longer sinks to the
    screen bottom. The toolbar keeps its own content height (dont_extend_height),
    so this is about which child sits last and holds the weight."""
    from prompt_toolkit.layout.containers import ConditionalContainer, Window

    from imp.cli.prompt import _build_session

    children = _build_session(None).layout.container.children

    # (b) the filler is the LAST child: a plain Window, not a ConditionalContainer
    filler = children[-1]
    assert isinstance(filler, Window)
    assert not isinstance(filler, ConditionalContainer)
    # (b) and it carries a high, expanding height weight (prompt_toolkit's default
    # window weight is 1, so a large weight wins the renderer's slack space)
    assert filler.height.weight >= 1000

    # (a) the child immediately before the filler is the bottom-toolbar container,
    # whose toolbar Window keeps its own content height (dont_extend_height=True)
    toolbar_cc = children[-2]
    assert isinstance(toolbar_cc, ConditionalContainer)
    assert isinstance(toolbar_cc.content, Window)
    assert toolbar_cc.content.style == "class:bottom-toolbar"
    assert toolbar_cc.content.dont_extend_height() is True

    # (b) the toolbar is no longer the last vertically-expanding child
    assert children[-1] is not toolbar_cc


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
    captured: dict[str, object] = {}

    class _SpySession:
        async def prompt_async(self, msg, **kwargs):  # type: ignore[no-untyped-def]
            captured.update(kwargs)
            return "ok"

    from prompt_toolkit.formatted_text import to_formatted_text
    from prompt_toolkit.styles.defaults import default_ui_style

    prompt = Prompt(color=True, _session_factory=lambda: _SpySession())
    await prompt.ask_async(bottom_toolbar="\x1b[35mmodel\x1b[39m")
    frags = to_formatted_text(captured["bottom_toolbar"])
    assert frags                                          # non-empty fragments
    # every fragment carries an inline noreverse; the ANSI magenta survives
    assert all("noreverse" in style for style, _text, *_ in frags)
    assert any("ansimagenta" in style for style, _text, *_ in frags)
    # inline noreverse defeats prompt_toolkit's default reverse-video bottom-toolbar
    # class regardless of class-merge precedence
    ui = default_ui_style()
    assert all(
        ui.get_attrs_for_style_str(f"class:bottom-toolbar {style}").reverse is False
        for style, _text, *_ in frags
    )
    assert "style" in captured                            # secondary noreverse class guard


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


@pytest.mark.anyio
async def test_ask_async_flushes_tty_input_before_reading(monkeypatch) -> None:
    """AC1/AC4: on a real tty, ask_async flushes the kernel input queue on
    sys.stdin's fd with TCIFLUSH, and the flush precedes the prompt_async read."""
    import sys
    import termios

    order: list[str] = []
    flush_args: list[tuple[int, int]] = []

    def _fake_tcflush(fd: int, queue: int) -> None:
        order.append("flush")
        flush_args.append((fd, queue))

    class _FakeStdin:
        def isatty(self) -> bool:
            return True

        def fileno(self) -> int:
            return 0

    monkeypatch.setattr(sys, "stdin", _FakeStdin())
    monkeypatch.setattr(termios, "tcflush", _fake_tcflush)

    class _SpySession:
        async def prompt_async(self, msg, **kwargs):  # type: ignore[no-untyped-def]
            order.append("read")
            return "answer"

    prompt = Prompt(color=False, _session_factory=lambda: _SpySession())
    result = await prompt.ask_async()

    assert result == "answer"
    assert order == ["flush", "read"]               # flush runs BEFORE the read
    assert flush_args == [(0, termios.TCIFLUSH)]     # stdin fd, input queue only


@pytest.mark.anyio
async def test_ask_async_does_not_flush_when_stdin_is_not_a_tty(monkeypatch) -> None:
    """AC2/AC4: when sys.stdin is not a tty, ask_async never flushes and never
    touches the fd; the read proceeds and returns normally."""
    import sys
    import termios

    flushed: list[tuple] = []
    monkeypatch.setattr(termios, "tcflush", lambda *a: flushed.append(a))

    class _FakeStdin:
        def isatty(self) -> bool:
            return False

        def fileno(self) -> int:
            raise AssertionError("fileno must not be read on a non-tty stdin")

    monkeypatch.setattr(sys, "stdin", _FakeStdin())

    class _SpySession:
        async def prompt_async(self, msg, **kwargs):  # type: ignore[no-untyped-def]
            return "typed"

    prompt = Prompt(color=False, _session_factory=lambda: _SpySession())
    result = await prompt.ask_async()

    assert result == "typed"     # read proceeds normally
    assert flushed == []         # no flush attempted on a non-tty fd


@pytest.mark.anyio
async def test_ask_async_headless_path_reads_without_flush(monkeypatch) -> None:
    """AC3/AC4: the headless / --message path (non-tty stdin) reads input
    unchanged with no flush and raises no error, even with termios available."""
    import sys
    import termios

    flushed: list[tuple] = []
    monkeypatch.setattr(termios, "tcflush", lambda *a: flushed.append(a))

    class _NonTtyStdin:
        def isatty(self) -> bool:
            return False

        def fileno(self) -> int:
            return 3

    monkeypatch.setattr(sys, "stdin", _NonTtyStdin())

    class _SpySession:
        async def prompt_async(self, msg, **kwargs):  # type: ignore[no-untyped-def]
            return "headless reply"

    prompt = Prompt(color=False, _session_factory=lambda: _SpySession())
    result = await prompt.ask_async()

    assert result == "headless reply"   # input read normally, no regression
    assert flushed == []                # never flushed on the headless path
