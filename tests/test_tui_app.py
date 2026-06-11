from __future__ import annotations

import pytest

from imp.cli.narrative import build_tree
from imp.cli.render_text import render_tree
from imp.cli.tui_app import HINT, TuiApp
from tests._event_fixtures import SAMPLE, ev


class _FakeCoord:
    def __init__(self) -> None:
        self.pauses: list[str] = []

    async def signal_pause(self, *, reason: str = "ctrl-c") -> None:
        self.pauses.append(reason)


def _app() -> TuiApp:
    return TuiApp(coord=_FakeCoord(), color=False, width=80)


def test_narrative_reflects_verbosity_and_matches_render_tree() -> None:
    app = _app()
    for e in SAMPLE:
        app.on_event(e)
    full = "\n".join(render_tree(build_tree(SAMPLE), full=True, color=False, width=80))
    lean = "\n".join(render_tree(build_tree(SAMPLE), full=False, color=False, width=80))
    assert full != lean                       # the fixture distinguishes the modes
    assert app.narrative() == full            # full=True initially
    app.toggle_verbosity()
    assert app.narrative() == lean
    app.toggle_verbosity()
    assert app.narrative() == full


def test_status_line_is_hint_only_when_idle() -> None:
    assert _app().status_line() == HINT


def test_status_line_includes_activity_and_hint_during_a_call() -> None:
    app = _app()
    app.on_event(ev("1", "step_started", step="s", kind="agent", phase="p"))
    app.on_event(ev("t1", "tool_call", agent_id="a", id="c1", name="bash",
                    input={"command": "ls"}))
    line = app.status_line()
    assert "bash ls" in line
    assert HINT in line


def test_scroll_page_up_disengages_follow() -> None:
    app = _app()
    app._metrics = lambda: (100, 20)          # type: ignore[method-assign]
    app.scroll_page_up()
    assert app._viewport.follow is False
    assert app._viewport.vertical_scroll(total_lines=100, height=20) == 60


def test_scroll_page_down_then_jump_to_bottom_reengages_follow() -> None:
    app = _app()
    app._metrics = lambda: (100, 20)          # type: ignore[method-assign]
    app.scroll_page_up()                       # follow off, top at 60
    app.scroll_page_down()                     # back to bottom (80)
    assert app._viewport.vertical_scroll(total_lines=100, height=20) == 80
    app.jump_to_bottom()
    assert app._viewport.follow is True


def test_scroll_wheel_lines_change_offset() -> None:
    app = _app()
    app._metrics = lambda: (100, 20)          # type: ignore[method-assign]
    app.scroll_up_lines()                      # default n=3 from bottom (80) -> 77
    assert app._viewport.vertical_scroll(total_lines=100, height=20) == 77
    app.scroll_down_lines()                    # +3 -> 80
    assert app._viewport.vertical_scroll(total_lines=100, height=20) == 80


@pytest.mark.anyio
async def test_pause_run_routes_esc_to_coordinator() -> None:
    coord = _FakeCoord()
    app = TuiApp(coord=coord, color=False, width=80)
    await app.pause_run()
    assert coord.pauses == ["esc"]


@pytest.mark.anyio
async def test_interrupt_run_routes_ctrl_c_to_coordinator() -> None:
    coord = _FakeCoord()
    app = TuiApp(coord=coord, color=False, width=80)
    await app.interrupt_run()
    assert coord.pauses == ["ctrl-c"]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
