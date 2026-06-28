from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from imp.cli import palette as pal
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
    full = "\n".join(render_tree(build_tree(SAMPLE), full=True, color=False,
                                 width=80, live_viewer=True))
    lean = "\n".join(render_tree(build_tree(SAMPLE), full=False, color=False,
                                 width=80, live_viewer=True))
    assert full != lean                       # the fixture distinguishes the modes
    assert app.narrative() == lean            # full=False initially (lean default)
    app.toggle_verbosity()
    assert app.narrative() == full
    app.toggle_verbosity()
    assert app.narrative() == lean


def test_status_line_is_hint_only_when_idle() -> None:
    assert _app().status_line() == HINT


def test_status_line_includes_activity_and_hint_during_a_call() -> None:
    app = TuiApp(coord=_FakeCoord(), color=False, width=120)
    app.on_event(ev("1", "step_started", step="s", kind="agent", phase="p"))
    app.on_event(ev("t1", "tool_call", agent_id="a", id="c1", name="bash",
                    input={"command": "ls"}))
    line = app.status_line()
    assert "bash ls" in line          # short activity fits at width 120; shown in full
    assert line.endswith(HINT)        # hint occupies the reserved tail


def test_long_activity_elides_to_preserve_hint_slot() -> None:
    app = TuiApp(coord=_FakeCoord(), color=False, width=80)
    app.on_event(ev("1", "step_started", step="s", kind="agent", phase="p"))
    app.on_event(ev("t1", "tool_call", agent_id="a", id="c1", name="bash",
                    input={"command": "x" * 120}))
    line = app.status_line()
    assert line.endswith(HINT)        # hint occupies the reserved tail, in full
    assert len(line) <= 80            # color off → visible length == len; nothing overflows


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
                Keys.ControlC, Keys.ScrollUp, Keys.ScrollDown):
        assert key in bound, key
    assert Keys.Escape not in bound          # ESC is no longer an interrupt gesture
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
    assert app._full is True                  # Ctrl-O toggled verbosity off the lean default


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


def test_resolved_reply_seeds_pending_echo_into_narrative() -> None:
    app = TuiApp(coord=_FakeCoord(), color=False, width=80)
    loop = asyncio.new_event_loop()
    try:
        app._prompt_future = loop.create_future()
        app._resolve_prompt("ship it")
        # The resolved text lands in the narrative the instant the box hides...
        assert "ship it" in app.narrative()
        # ...and the awaiting future still resolves to that exact value.
        assert app._prompt_future.result() == "ship it"
    finally:
        loop.close()


def test_opening_intent_seeded_shows_in_narrative_from_frame_one() -> None:
    app = TuiApp(coord=_FakeCoord(), color=False, width=80,
                 seed_opening="add a --json flag")
    # Before any daemon event is processed, the typed text already renders.
    assert "add a --json flag" in app.narrative()


def test_no_opening_intent_seeds_nothing() -> None:
    app = TuiApp(coord=_FakeCoord(), color=False, width=80)
    assert app.narrative() == ""


def test_status_line_reads_your_turn_during_solicit_not_spinner() -> None:
    app = _app()
    app.on_event(ev("1", "step_started", step="s", kind="agent", phase="p"))
    app.on_event(ev("t1", "tool_call", agent_id="a", id="c1", name="bash",
                    input={"command": "ls"}))
    app._prompt_visible = True
    line = app.status_line()
    assert "your turn" in line
    assert "bash ls" not in line            # the spinner activity is replaced
    assert line.endswith(HINT)


def test_your_turn_status_line_shows_blue_accent_marker() -> None:
    app = TuiApp(coord=_FakeCoord(), color=True, width=80)
    app._prompt_visible = True
    line = app.status_line()
    assert "> your turn" in line       # bright-blue caret marker, not a full stripe
    assert pal.sgr("bright_blue", enabled=True) in line  # bright_blue accent (imp's user identity)
    assert "\x1b[7m" not in line       # no inverse fill in the your-turn text


def test_status_window_reverse_fill_is_state_dependent() -> None:
    app = _app()
    app._build_app()
    status_win = app._status_win
    app._prompt_visible = False
    assert status_win.style() == "reverse"   # running/idle keeps the inverse fill
    app._prompt_visible = True
    assert status_win.style() == ""           # your turn drops the fill


def test_question_window_wraps_and_docks_to_content_height() -> None:
    from prompt_toolkit.layout.containers import Window

    app = _app()
    app._build_app()
    qwin = next(w for w in app._app.layout.walk()
                if isinstance(w, Window) and w.content is app._question_control)
    assert qwin.wrap_lines() is True
    assert qwin.dont_extend_height() is True


def test_prompt_window_shows_caret_and_question() -> None:
    from prompt_toolkit.formatted_text import ANSI
    from prompt_toolkit.layout.processors import BeforeInput

    from imp.cli import palette as pal

    app = _app()
    app._build_app()
    procs = app._prompt_control.input_processors
    assert any(isinstance(p, BeforeInput) and p.text == pal.CARET for p in procs)

    app._solicit_question = "Really deploy to prod?"
    got = app._question_control.text()
    assert isinstance(got, ANSI) and "Really deploy to prod?" in got.value


@pytest.mark.anyio
async def test_solicit_shows_your_turn_status(tmp_path: Path) -> None:
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input.defaults import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    app = TuiApp(coord=_FakeCoord(), color=False, width=80)
    app.on_event(ev("1", "step_started", step="s", kind="agent", phase="p"))
    app.on_event(ev("2", "awaiting_user_input", agent_id="step:p/s",
                    prompt="Ship it?"))
    change = _FakeChange(tmp_path)
    captured: dict[str, str] = {}

    with create_pipe_input() as pin, create_app_session(input=pin, output=DummyOutput()):
        app._build_app()

        async def _ask() -> None:
            await app.solicit(change)
            app.exit()

        async def _drive() -> None:
            await asyncio.sleep(0.05)
            captured["status"] = app.status_line()
            pin.send_text("yes\r")

        asyncio.ensure_future(_ask())   # noqa: RUF006
        asyncio.ensure_future(_drive())  # noqa: RUF006
        await app.run_async()

    assert "your turn" in captured["status"]
    assert app._solicit_question == ""      # reset after solicit returns
