# tests/test_prompt_ctrl_c_binding.py
from __future__ import annotations

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from imp.cli.prompt import _build_session


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_ctrl_c_with_text_clears_buffer_no_exception() -> None:
    with create_pipe_input() as pipe:
        with create_app_session(input=pipe, output=DummyOutput()):
            session = _build_session()
            pipe.send_text("hello")        # type text
            pipe.send_text("\x03")         # Ctrl-C → must clear, NOT raise
            pipe.send_text("world\r")      # type more, submit
            result = await session.prompt_async("> ")
    assert result == "world"               # 'hello' was cleared, no KeyboardInterrupt
