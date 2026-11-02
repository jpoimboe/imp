from __future__ import annotations

import asyncio
import contextlib
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from imp.cli.prompt import _build_session
from impty import TerminalSession
from tests.impty.e2e_support import can_pty, clean_env


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


async def _drive_keys(skills_dir: Path, editing_mode: str, keys: list[str]) -> str:
    """Feed keys while the prompt runs; Tab waits for completions of the text
    typed so far. Times out if Enter never submits."""
    with create_pipe_input() as pipe, create_app_session(
        input=pipe, output=DummyOutput()
    ):
        session = _build_session(skills_dir, editing_mode=editing_mode)
        buffer = session.default_buffer

        async def completions_ready(typed: str) -> None:
            while not (
                buffer.text == typed
                and buffer.complete_state
                and buffer.complete_state.original_document.text == typed
                and buffer.complete_state.completions
            ):
                await asyncio.sleep(0.01)

        async def drive() -> str:
            task = asyncio.create_task(session.prompt_async("> "))
            try:
                for i, key in enumerate(keys):
                    if key == "\t":
                        await completions_ready("".join(keys[:i]))
                    await asyncio.sleep(0.01)
                    pipe.send_text(key)
                return str(await task)
            finally:
                if not task.done():
                    task.cancel()

        return await asyncio.wait_for(drive(), timeout=10)


@pytest.mark.anyio
@pytest.mark.parametrize("editing_mode", ["vi", "emacs"])
async def test_enter_submits_selected_completion_first_press(
    tmp_path: Path, editing_mode: str
) -> None:
    # Builtins only: the first `/sa` candidate is `/save`.
    result = await _drive_keys(tmp_path, editing_mode, ["/", "s", "a", "\t", "\r"])
    assert result == "/save"
    assert "\n" not in result


@pytest.mark.anyio
@pytest.mark.parametrize("editing_mode", ["vi", "emacs"])
async def test_enter_submits_tab_completed_skills(
    tmp_path: Path, editing_mode: str
) -> None:
    result = await _drive_keys(tmp_path, editing_mode, ["/", "s", "k", "\t", "\r"])
    assert result == "/skills"


_PROMPT_PROGRAM = (
    "import asyncio\n"
    "from imp.cli.prompt import _build_session\n"
    "async def _main() -> None:\n"
    "    session = _build_session()\n"
    "    await session.prompt_async('> ', bottom_toolbar='\\nstatus')\n"
    "asyncio.run(_main())\n"
)


@contextlib.contextmanager
def _prompt_pty() -> Iterator[TerminalSession]:
    """Run `_build_session().prompt_async` under a real CPR-answering pty
    (impty) and yield the settled session. Unlike DummyOutput — which never
    answers the cursor-position request, so the renderer's reserved height stays
    0 and the toolbar collapses onto a tiny screen regardless of the filler —
    impty faithfully replies to CPR, so the renderer reserves the real terminal
    height and the bottom-toolbar positioning matches production."""
    env = clean_env()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    with TerminalSession(
        [sys.executable, "-c", _PROMPT_PROGRAM], env=env, cols=80, rows=40
    ) as session:
        session.wait_idle(idle=0.5, hard=6.0)
        yield session


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_toolbar_hugs_input_one_blank_line_below_in_real_terminal() -> None:
    """AC1/AC2: driving the REAL rendering path (a CPR-answering pty), a
    single-row input places the toolbar one blank line below the input line
    (input, blank, toolbar) — not on the terminal's last row. The unconditional
    filler absorbs the renderer's full-height fill so this hug appears; without
    it the toolbar sinks to the terminal's last row (row 39)."""
    with _prompt_pty() as session:
        lines, rows = session.lines(limit=40), session.rows
    input_row = next(i for i, ln in enumerate(lines) if ln.strip().startswith(">"))
    toolbar_row = next(i for i, ln in enumerate(lines) if "status" in ln)
    assert lines[input_row + 1].strip() == ""
    assert toolbar_row == input_row + 2
    assert toolbar_row < rows - 2


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_toolbar_hugs_wrapped_multiline_input_in_real_terminal() -> None:
    """AC1: with the unconditional filler, an input that wraps to two+ visual
    rows keeps the toolbar one blank line below the input's LAST row — it no
    longer sinks to the terminal's bottom rows when the input grows. `> ` + 100
    'x's on an 80-col terminal wraps to exactly two rows (rows 0 and 1), so the
    blank line lands on row 2 and the toolbar on row 3, well above the bottom."""
    with _prompt_pty() as session:
        session.send("x" * 100)
        session.wait_idle(idle=0.5, hard=6.0)
        rows = session.rows
        lines = session.lines(limit=40)
    toolbar_row = next(i for i, ln in enumerate(lines) if "status" in ln)
    last_input_row = 1
    assert lines[0].startswith("> x")
    assert lines[1].lstrip().startswith("x")
    assert lines[last_input_row + 1].strip() == ""
    assert toolbar_row == last_input_row + 2
    assert toolbar_row < rows - 2
