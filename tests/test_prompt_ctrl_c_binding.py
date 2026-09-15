# tests/test_prompt_ctrl_c_binding.py
from __future__ import annotations

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from imp.cli.prompt import Prompt, _build_session


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
    assert EMPTY_PROMPT_HINT == "press ctrl-d to pause & exit"


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
    assert "press ctrl-d to pause & exit" in capsys.readouterr().out


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
