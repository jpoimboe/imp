# tests/test_prompt_ctrl_c_binding.py
from __future__ import annotations

import asyncio

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import PipeInput, create_pipe_input
from prompt_toolkit.output import DummyOutput

from imp.cli.prompt import (
    DEFAULT_HINT,
    EMPTY_PROMPT_HINT,
    OPENING_HINTS,
    Prompt,
    _build_session,
)
from imp.cli.signals import _feed_prompt_ctrl_c


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_ctrl_c_with_text_clears_buffer_no_exception() -> None:
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        session = _build_session()
        pipe.send_text("hello")        # type text
        pipe.send_text("\x03")         # Ctrl-C → must clear, NOT raise
        pipe.send_text("world\r")      # type more, submit
        result = await session.prompt_async("> ")
    assert result == "world"               # 'hello' was cleared, no KeyboardInterrupt


@pytest.mark.anyio
async def test_ctrl_c_on_empty_buffer_does_not_raise() -> None:
    # The spec-mandated crash-fix path: Ctrl-C on an EMPTY buffer must not raise
    # (the default binding raises KeyboardInterrupt); the prompt keeps going.
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        session = _build_session()
        pipe.send_text("\x03")         # Ctrl-C on empty → hint, NOT raise
        pipe.send_text("world\r")      # prompt still alive → submit
        result = await session.prompt_async("> ")
    assert result == "world"


@pytest.mark.anyio
async def test_ask_async_passes_handle_sigint_false() -> None:
    # handle_sigint=False keeps prompt_toolkit from owning SIGINT during the
    # prompt, so an out-of-band SIGINT lands in imp's InterruptCoordinator loop
    # handler and routes through the exit path (instead of a KBI that crosses the
    # claude MCP boundary and crashes anyio.run).
    captured: dict[str, object] = {}

    class _SpySession:
        async def prompt_async(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            captured.update(kwargs)
            return "ok"

    prompt = Prompt(_session_factory=lambda: _SpySession())
    line = await prompt.ask_async()
    assert line == "ok"
    assert captured.get("handle_sigint") is False


def test_ask_sync_passes_handle_sigint_false() -> None:
    # The sync path is not used interactively, but it must stay consistent.
    captured: dict[str, object] = {}

    class _SpySession:
        def prompt(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            captured.update(kwargs)
            return "ok"

    prompt = Prompt(_session_factory=lambda: _SpySession())
    assert prompt.ask() == "ok"
    assert captured.get("handle_sigint") is False


def test_empty_prompt_hint_names_pause_and_exit() -> None:
    from imp.cli.prompt import EMPTY_PROMPT_HINT
    assert EMPTY_PROMPT_HINT == "press Ctrl-D to pause & exit"


@pytest.mark.anyio
async def test_ctrl_c_on_empty_buffer_prints_the_sessions_hint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    # Run the hint print synchronously inside the key handler: prompt_toolkit
    # would otherwise schedule it and this assertion would race the scheduler.
    monkeypatch.setattr(
        "prompt_toolkit.application.run_in_terminal", lambda fn: fn())
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        session = _build_session(empty_hint="press ctrl-d to cancel setup")
        pipe.send_text("\x03")          # Ctrl-C on an empty buffer
        pipe.send_text("world\r")
        assert await session.prompt_async("> ") == "world"
    assert "press ctrl-d to cancel setup" in capsys.readouterr().out


@pytest.mark.anyio
async def test_ctrl_c_hint_defaults_to_the_run_paths_wording(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "prompt_toolkit.application.run_in_terminal", lambda fn: fn())
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        session = _build_session()
        pipe.send_text("\x03")
        pipe.send_text("world\r")
        assert await session.prompt_async("> ") == "world"
    assert "press Ctrl-D to pause & exit" in capsys.readouterr().out


@pytest.mark.anyio
async def test_ctrl_c_hint_goes_through_the_sessions_output_fn(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "prompt_toolkit.application.run_in_terminal", lambda fn: fn())
    rows: list[str] = []
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        session = _build_session(output_fn=rows.append)
        pipe.send_text("\x03")
        pipe.send_text("world\r")
        assert await session.prompt_async("> ") == "world"
    assert rows == ["press Ctrl-D to pause & exit", ""]
    assert capsys.readouterr().out == ""


def test_prompt_threads_its_output_fn_into_the_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from imp.cli import prompt as prompt_mod

    captured: dict[str, object] = {}

    def _fake_build(skills_dir=None, **kwargs):
        captured.update(kwargs)
        return object()

    def _output(_line: str) -> None: ...

    monkeypatch.setattr(prompt_mod, "_build_session", _fake_build)
    Prompt(output_fn=_output)
    assert captured["output_fn"] is _output


def test_prompt_threads_its_empty_hint_into_the_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from imp.cli import prompt as prompt_mod

    captured: dict[str, object] = {}

    def _fake_build(skills_dir=None, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(prompt_mod, "_build_session", _fake_build)
    Prompt(empty_hint="press ctrl-d to cancel setup")
    assert captured["empty_hint"] == "press ctrl-d to cancel setup"


GATE_PLACEHOLDERS = [
    "interrupted · enter to continue · type to redirect · Ctrl-D to leave",
    "paused · enter to continue · type to redirect · Ctrl-D to leave",
    "paused · enter to re-run the gate · Ctrl-D to leave",
]
HINTED_PLACEHOLDERS = [DEFAULT_HINT, *OPENING_HINTS.values()]


@pytest.fixture
def sync_hint_print(monkeypatch: pytest.MonkeyPatch) -> None:
    # Run the hint print inside the key handler so assertions don't race the scheduler.
    monkeypatch.setattr("prompt_toolkit.application.run_in_terminal", lambda fn: fn())


async def _ctrl_c_then_reply(prompt: Prompt, pipe: PipeInput, hint: str) -> None:
    pipe.send_text("\x03reply\r")
    assert await prompt.ask_async(hint=hint) == "reply"


async def _sigint_then_reply(prompt: Prompt, pipe: PipeInput, hint: str) -> None:
    async def _feed() -> None:
        while not prompt._session.app.is_running:
            await asyncio.sleep(0.01)
        _feed_prompt_ctrl_c()
        pipe.send_text("reply\r")

    feeder = asyncio.create_task(_feed())
    assert await prompt.ask_async(hint=hint) == "reply"
    await feeder


@pytest.mark.anyio
@pytest.mark.usefixtures("sync_hint_print")
@pytest.mark.parametrize("color", [False, True])
async def test_ctrl_c_on_empty_line_is_silent_at_every_gate_placeholder(
    color: bool, capsys: pytest.CaptureFixture[str],
) -> None:
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        prompt = Prompt(color=color)
        for gate in GATE_PLACEHOLDERS:
            await _ctrl_c_then_reply(prompt, pipe, gate)
    assert capsys.readouterr().out == ""


@pytest.mark.anyio
@pytest.mark.usefixtures("sync_hint_print")
@pytest.mark.parametrize("gate", GATE_PLACEHOLDERS)
async def test_gate_ctrl_c_does_not_spend_the_hint(
    gate: str, capsys: pytest.CaptureFixture[str],
) -> None:
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        prompt = Prompt()
        await _ctrl_c_then_reply(prompt, pipe, gate)
        assert capsys.readouterr().out == ""
        await _ctrl_c_then_reply(prompt, pipe, DEFAULT_HINT)
    assert capsys.readouterr().out == f"{EMPTY_PROMPT_HINT}\n\n"


@pytest.mark.anyio
@pytest.mark.usefixtures("sync_hint_print")
@pytest.mark.parametrize("color", [False, True])
@pytest.mark.parametrize("placeholder", HINTED_PLACEHOLDERS)
async def test_ctrl_c_hints_once_per_prompt_at_placeholders_not_naming_ctrl_d(
    placeholder: str, color: bool, capsys: pytest.CaptureFixture[str],
) -> None:
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        prompt = Prompt(color=color)
        await _ctrl_c_then_reply(prompt, pipe, placeholder)
        assert capsys.readouterr().out == f"{EMPTY_PROMPT_HINT}\n\n"
        await _ctrl_c_then_reply(prompt, pipe, placeholder)
    assert capsys.readouterr().out == ""


@pytest.mark.anyio
@pytest.mark.usefixtures("sync_hint_print")
async def test_ctrl_c_with_text_at_gate_clears_the_line_silently(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        prompt = Prompt()
        pipe.send_text("hello\x03world\r")
        assert await prompt.ask_async(hint=GATE_PLACEHOLDERS[0]) == "world"
    assert capsys.readouterr().out == ""


@pytest.mark.anyio
@pytest.mark.usefixtures("sync_hint_print")
async def test_out_of_band_sigint_is_silent_at_gate_and_hints_elsewhere(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        prompt = Prompt()
        await _sigint_then_reply(prompt, pipe, GATE_PLACEHOLDERS[0])
        assert capsys.readouterr().out == ""
        # Proves the feed reaches the binding, so the silence above is not vacuous.
        await _sigint_then_reply(prompt, pipe, DEFAULT_HINT)
    assert capsys.readouterr().out == f"{EMPTY_PROMPT_HINT}\n\n"


@pytest.mark.anyio
@pytest.mark.usefixtures("sync_hint_print")
@pytest.mark.parametrize("gate", GATE_PLACEHOLDERS)
async def test_ctrl_d_still_leaves_a_gate_after_a_silent_ctrl_c(gate: str) -> None:
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        prompt = Prompt()
        pipe.send_text("\x03\x04")
        with pytest.raises(EOFError):
            await prompt.ask_async(hint=gate)


def test_gate_placeholder_constants_match_the_silenced_strings() -> None:
    from imp.engine.completion import INTERRUPT_STEER_HINT
    from imp.remote.session.daemon import _RESUME_PROMPT, _SCRIPT_HOLD_PROMPT
    assert [INTERRUPT_STEER_HINT, _RESUME_PROMPT, _SCRIPT_HOLD_PROMPT] == GATE_PLACEHOLDERS
