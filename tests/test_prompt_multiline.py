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
async def test_alt_enter_inserts_newline_then_enter_submits() -> None:
    # Alt+Enter is Esc+Enter on the wire: ESC (\x1b) then CR (\r).
    with create_pipe_input() as pipe, create_app_session(
        input=pipe, output=DummyOutput()
    ):
        session = _build_session()
        pipe.send_text("first line")
        pipe.send_text("\x1b\r")          # Alt+Enter -> newline
        pipe.send_text("second line\r")   # Enter -> submit
        result = await session.prompt_async("> ")
    assert result == "first line\nsecond line"


@pytest.mark.anyio
async def test_plain_enter_submits_single_line() -> None:
    # Regression guard: multiline=True must not make a bare Enter insert a
    # newline — Enter still submits a single-line message.
    with create_pipe_input() as pipe, create_app_session(
        input=pipe, output=DummyOutput()
    ):
        session = _build_session()
        pipe.send_text("just one line\r")
        result = await session.prompt_async("> ")
    assert result == "just one line"


@pytest.mark.anyio
async def test_ctrl_j_submits_single_line() -> None:
    # c-j (LF) must submit just like c-m (CR), for terminals that send LF for Enter.
    with create_pipe_input() as pipe, create_app_session(
        input=pipe, output=DummyOutput()
    ):
        session = _build_session()
        pipe.send_text("hello\n")
        result = await session.prompt_async("> ")
    assert result == "hello"
