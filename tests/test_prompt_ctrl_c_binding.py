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
    # claude_code MCP boundary and crashes anyio.run).
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
