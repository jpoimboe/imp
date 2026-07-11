from __future__ import annotations

import asyncio

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


def _row_text(screen: object, row: int, cols: int = 80) -> str:
    buf = screen.data_buffer  # type: ignore[attr-defined]
    return "".join(buf[row][c].char for c in range(cols)).rstrip()


@pytest.mark.anyio
async def test_toolbar_floats_one_blank_line_below_input_not_screen_bottom() -> None:
    """AC(c): on an empty-scrollback 40-row terminal, the mid-prompt (non-done)
    screen places the toolbar text one blank line below the input line, not on
    the terminal's last row. Without the filler the toolbar pins to row 39."""
    with create_pipe_input() as pipe, create_app_session(
        input=pipe, output=DummyOutput()
    ):
        session = _build_session()
        pipe.send_text("hello")  # typed, deliberately NOT submitted
        # ask_async prefixes the toolbar with a single "\n"; mimic that here so the
        # one-blank-line spacing (unchanged by this change) is exercised too.
        task = asyncio.create_task(
            session.prompt_async("> ", bottom_toolbar="\nstatus")
        )
        try:
            screen = None
            for _ in range(500):
                await asyncio.sleep(0.005)
                candidate = session.app.renderer._last_screen
                if candidate is not None and any(
                    "hello" in _row_text(candidate, r)
                    for r in range(candidate.height)
                ):
                    screen = candidate
                    break
            assert screen is not None, "prompt never rendered the typed input"

            rows = session.app.output.get_size().rows  # DummyOutput -> 40
            input_row = next(
                r for r in range(screen.height) if "hello" in _row_text(screen, r)
            )
            toolbar_row = next(
                r for r in range(screen.height) if "status" in _row_text(screen, r)
            )

            # exactly one blank line separates the input from the toolbar
            assert _row_text(screen, input_row + 1) == ""
            assert toolbar_row == input_row + 2
            # and the toolbar is NOT pinned to the terminal's last row
            assert toolbar_row < rows - 2
        finally:
            if session.app.is_running:
                session.app.exit(result="")
            await task
