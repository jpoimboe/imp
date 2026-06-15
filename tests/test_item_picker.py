from __future__ import annotations

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from imp.cli.resume_select import _ItemPicker, run_item_picker


def test_render_marks_selection_with_render_item() -> None:
    p = _ItemPicker(["alpha", "beta"], lambda s: s.upper(), color=False)
    lines = p.render().splitlines()
    assert lines[0] == "▸ ALPHA"
    assert lines[1] == "  BETA"


def test_move_clamps_and_selects() -> None:
    p = _ItemPicker(["a", "b"], lambda s: s, color=False)
    p.move_up()                      # already top → clamp
    assert p.selected() == "a"
    p.move_down()
    p.move_down()                    # already bottom → clamp
    assert p.selected() == "b"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_enter_returns_preselected_first() -> None:
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        pipe.send_text("\r")
        chosen = await run_item_picker(
            ["a", "b"], render_item=lambda s: s, header="h", color=False)
    assert chosen == "a"


@pytest.mark.anyio
async def test_down_then_enter_returns_second() -> None:
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        pipe.send_text("\x1b[B")     # down arrow
        pipe.send_text("\r")
        chosen = await run_item_picker(
            ["a", "b"], render_item=lambda s: s, header="h", color=False)
    assert chosen == "b"


@pytest.mark.anyio
async def test_q_cancels_to_none() -> None:
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        pipe.send_text("q")
        chosen = await run_item_picker(
            ["a", "b"], render_item=lambda s: s, header="h", color=False)
    assert chosen is None
