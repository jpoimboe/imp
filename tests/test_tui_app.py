from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

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


def test_build_app_registers_all_bindings() -> None:
    from prompt_toolkit.keys import Keys

    app = _app()
    app._build_app()
    bound = {k for b in app._app.key_bindings.bindings for k in b.keys}
    for key in (Keys.PageUp, Keys.PageDown, Keys.End, Keys.ControlO,
                Keys.Escape, Keys.ControlC, Keys.ScrollUp, Keys.ScrollDown):
        assert key in bound, key
    assert app._app.full_screen is True
    assert app._app.mouse_support() is True


@pytest.mark.anyio
async def test_run_async_pageup_disengages_follow_and_ctrl_o_toggles() -> None:
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input.defaults import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    app = TuiApp(coord=_FakeCoord(), color=False, width=80)
    for e in SAMPLE:
        app.on_event(e)

    with create_pipe_input() as pin, create_app_session(input=pin, output=DummyOutput()):
        async def _drive() -> None:
            pin.send_text("\x1b[5~")          # PageUp
            pin.send_text("\x0f")             # Ctrl-O
            await asyncio.sleep(0.05)
            app.exit()

        asyncio.ensure_future(_drive())  # noqa: RUF006
        await app.run_async()

    assert app._viewport.follow is False      # PageUp disengaged auto-follow
    assert app._full is False                 # Ctrl-O toggled verbosity


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _FakeBuffer:
    def __init__(self) -> None:
        self.text = ""
        self.completer: object | None = None

    def reset(self) -> None:
        self.text = ""


class _FakeChange:
    def __init__(self, tmp_path: Any) -> None:
        self.workflow_dir = tmp_path
        (tmp_path / "skills").mkdir(exist_ok=True)


class _FakeFuture:
    """A loop-free stand-in for asyncio.Future for the sync _accept tests
    (no running event loop in a plain test)."""

    def __init__(self) -> None:
        self._done = False
        self._value: str | None = None

    def done(self) -> bool:
        return self._done

    def set_result(self, value: str) -> None:
        self._done = True
        self._value = value

    def result(self) -> str | None:
        return self._value


def test_accept_plain_line_resolves_solicit_future() -> None:
    app = _app()
    app._buffer = _FakeBuffer()
    app._change = _FakeChange  # not used on the plain path
    fut = _FakeFuture()
    app._prompt_future = fut
    app._buffer.text = "ship it"
    keep = app._accept(app._buffer)
    assert keep is False                       # buffer cleared on submit
    assert fut.result() == "ship it"


def test_accept_help_slash_reprompts_and_records_output(tmp_path: Path) -> None:
    app = _app()
    app._buffer = _FakeBuffer()
    app._change = _FakeChange(tmp_path)
    fut = _FakeFuture()
    app._prompt_future = fut
    app._buffer.text = "/help"
    keep = app._accept(app._buffer)
    assert keep is False
    assert not fut.done()                       # /help re-prompts, does not resolve
    assert any("slash command" in line.lower() for line in app._slash_lines)


@pytest.mark.anyio
async def test_solicit_shows_prompt_returns_submitted_line(tmp_path: Path) -> None:
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input.defaults import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    app = TuiApp(coord=_FakeCoord(), color=False, width=80)
    app.on_event(SAMPLE[0])
    change = _FakeChange(tmp_path)
    result: dict[str, str] = {}

    with create_pipe_input() as pin, create_app_session(input=pin, output=DummyOutput()):
        app._build_app()

        async def _ask() -> None:
            result["line"] = await app.solicit(change)
            app.exit()

        async def _drive() -> None:
            await asyncio.sleep(0.05)
            assert app._prompt_visible is True
            pin.send_text("go ahead\r")

        asyncio.ensure_future(_ask())   # noqa: RUF006
        asyncio.ensure_future(_drive())  # noqa: RUF006
        await app.run_async()

    assert result["line"] == "go ahead"
