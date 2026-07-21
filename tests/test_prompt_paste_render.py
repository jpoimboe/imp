"""Regression: a multi-line bracketed paste must render in well under a
generous budget. Pre-fix the gated filler's weight of 100000 made a single
screen-filling paste render take ~20s (take_using_weights costs ~weight x
visual rows); at weight 100 the same render takes milliseconds."""

from __future__ import annotations

import asyncio
import contextlib
import time

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from imp.cli.prompt import _build_session

RENDER_BUDGET_S = 2.0
_SCREEN_ROWS = 40
_PASTE_LINES = 60  # exceeds the 40-row screen so the input fills it
_OUTER_DEADLINE_S = 45.0  # single outer guard; below the suite's 60s signal timeout


class _FixedOutput(DummyOutput):
    def get_size(self) -> Size:
        return Size(rows=_SCREEN_ROWS, columns=80)


def _bracketed_paste(n_lines: int) -> str:
    body = "\n".join(
        f"line {i} with some words here to fill the row width" for i in range(n_lines))
    return "\x1b[200~" + body + "\x1b[201~"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_multiline_paste_renders_within_budget() -> None:
    with create_pipe_input() as pipe, create_app_session(
        input=pipe, output=_FixedOutput()
    ):
        session = _build_session()
        renderer = session.app.renderer
        original_render = renderer.render
        render_seconds = 0.0
        render_calls = 0

        def _timed_render(*args, **kwargs):
            nonlocal render_seconds, render_calls
            start = time.perf_counter()
            result = original_render(*args, **kwargs)
            render_seconds += time.perf_counter() - start
            render_calls += 1
            return result

        renderer.render = _timed_render

        task = asyncio.ensure_future(session.prompt_async("> "))
        while not session.app.is_running:
            await asyncio.sleep(0.001)

        # Measure only the paste-triggered renders.
        render_seconds = 0.0
        render_calls = 0
        pipe.send_text(_bracketed_paste(_PASTE_LINES))

        deadline = time.perf_counter() + _OUTER_DEADLINE_S
        # Poll until the paste lands in the buffer (the app task does the render).
        while session.default_buffer.text.count("\n") + 1 < _PASTE_LINES:
            assert time.perf_counter() < deadline, "paste never reflected in buffer"
            await asyncio.sleep(0)
        # Poll until renders go quiet (no new render for a short window).
        last_calls = -1
        quiet_since: float | None = None
        while True:
            assert time.perf_counter() < deadline, "renders never settled"
            await asyncio.sleep(0.01)
            if render_calls == last_calls:
                if quiet_since is None:
                    quiet_since = time.perf_counter()
                elif time.perf_counter() - quiet_since > 0.1:
                    break
            else:
                last_calls = render_calls
                quiet_since = None

        session.app.exit()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, timeout=5)

    assert render_seconds < RENDER_BUDGET_S, (
        f"paste render took {render_seconds:.2f}s (budget {RENDER_BUDGET_S}s)")
