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


def test_default_caret_is_empty() -> None:
    """The default caret is empty — the ┃ bar (added by the caller as the gutter)
    is the sole user-turn marker — so _message(None) carries no caret text or
    escapes in either color mode. An explicit prompt_text is still styled."""
    from prompt_toolkit.formatted_text import ANSI

    on = Prompt(color=True)._message(None)
    assert isinstance(on, ANSI)
    assert on.value == ""
    assert Prompt(color=False)._message(None) == ""
    # An explicit prompt_text is still rendered bright-blue under color.
    styled = Prompt(color=True)._message("> ")
    assert "\x1b[94m" in styled.value and ">" in styled.value


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
    # No question and an empty caret → the message line is empty (the caller's
    # ┃ gutter is the only marker).
    assert _prompt_message_text("", color=False) == ""


def test_message_puts_the_question_above_the_caret_plain() -> None:
    # The question sits on its own line; the input line below it is the bare
    # (now empty) caret.
    assert _prompt_message_text("Pick A or B?", color=False) == "Pick A or B?\n"


def test_message_strips_question_whitespace_plain() -> None:
    assert _prompt_message_text("  Pick A or B?  ", color=False) == "Pick A or B?\n"


def test_message_renders_markdown_question_above_the_caret() -> None:
    msg = _prompt_message_text("Reply **merge** to merge.", color=True, width=80)
    assert "\x1b[1m" in msg                          # bold for **merge**
    assert "**" not in msg                           # asterisks consumed
    assert msg.endswith("\n")                         # empty caret line last


def test_message_no_color_renders_markdown_without_ansi() -> None:
    msg = _prompt_message_text("Reply **merge** to merge.", color=False, width=80)
    assert "**" not in msg      # asterisks consumed by markdown
    assert "\x1b[" not in msg   # no ANSI codes on the no-color path
    assert "merge" in msg       # content present
    assert msg.endswith("\n")   # empty caret line last (no `> `)


def test_message_plain_question_still_sits_above_the_caret() -> None:
    assert _prompt_message_text("Pick A or B?", color=False, width=80) \
        == "Pick A or B?\n"   # input line below is the now-empty caret


def test_placeholder_hint_is_the_dim_reply_prompt() -> None:
    assert _placeholder_text(color=False) == "type your reply"
    assert _placeholder_text(color=True) == "\x1b[2mtype your reply\x1b[0m"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_ask_async_caret_at_content_column_with_gutter() -> None:
    captured: dict[str, object] = {}

    class _SpySession:
        async def prompt_async(self, msg, **kwargs):  # type: ignore[no-untyped-def]
            captured["msg"] = msg
            captured.update(kwargs)
            return "ok"

    prompt = Prompt(color=False, _session_factory=lambda: _SpySession())
    line = await prompt.ask_async(indent="  ")
    assert line == "ok"
    # indent (2) + 2-col gutter (two spaces, color off) → body text at column 4
    # (empty caret).
    assert captured["msg"] == "    "
    cont = captured["prompt_continuation"]
    # Pad = len(indent)+2 (gutter) +len(CARET)=0 → 4 cols, so continuation text
    # lands under the first line's body (no shift on submit).
    assert cont(80, 1, False) == "    "


@pytest.mark.anyio
async def test_ask_async_gutter_at_root_when_no_indent() -> None:
    captured: dict[str, object] = {}

    class _SpySession:
        async def prompt_async(self, msg, **kwargs):  # type: ignore[no-untyped-def]
            captured["msg"] = msg
            captured.update(kwargs)
            return ""

    prompt = Prompt(color=False, _session_factory=lambda: _SpySession())
    await prompt.ask_async()
    assert captured["msg"] == "  "                         # 2-col gutter, body at col 2
    # Pad = len(indent)=0 +2 (gutter) +len(CARET)=0 → 2 cols, under the body.
    assert captured["prompt_continuation"](80, 1, False) == "  "


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
    # The question block and the (empty) caret line are both prefixed with
    # indent + gutter.
    assert captured["msg"] == "    Merge it?\n    "
    # Pad = len(indent)=2 +2 (gutter) +len(CARET)=0 → 4 cols, under the body.
    assert captured["prompt_continuation"](80, 1, False) == "    "
