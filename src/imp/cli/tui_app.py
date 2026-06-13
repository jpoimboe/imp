from __future__ import annotations

import asyncio
import contextlib
import re
import time
from typing import Any, cast

from imp.cli.prompt import SlashCompleter
from imp.cli.slash import REPROMPT, handle_slash_line, is_slash_command
from imp.cli.tui_driver import TuiDriver
from imp.cli.tui_viewport import ViewportState

HINT = "PgUp/PgDn scroll · End follow · ^O verbosity · ESC/^C pause"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_STATUS_SEP = "   "


def _elide_to_width(text: str, budget: int) -> str:
    if budget <= 0:
        return ""
    visible = _ANSI_RE.sub("", text)
    if len(visible) <= budget:
        return text
    return visible[:budget - 1] + "…"


class TuiApp:
    def __init__(self, *, coord: Any, color: bool, width: int) -> None:
        self._coord = coord
        self._color = color
        self._width = width
        self._full = False
        self._frame = 0
        self._start = time.monotonic()
        self._viewport = ViewportState()
        self._driver = TuiDriver(
            color=color, width=width, on_invalidate=self._invalidate)
        self._narrative_text: str | None = None
        self._app: Any = None
        self._body: Any = None
        self._buffer: Any = None
        self._change: Any = None
        self._prompt_visible = False
        self._prompt_future: Any = None
        self._slash_lines: list[str] = []

    # -- event intake ---------------------------------------------------------
    def on_event(self, event: Any) -> None:
        self._driver.on_event(event)

    # -- narrative / status / verbosity --------------------------------------
    def narrative(self) -> str:
        if self._narrative_text is None:
            self._narrative_text = self._driver.render(full=self._full)
        return self._narrative_text

    def toggle_verbosity(self) -> None:
        self._full = not self._full
        self._invalidate()

    def _elapsed(self) -> float:
        return time.monotonic() - self._start

    def status_line(self) -> str:
        activity = self._driver.status_text(self._frame, self._elapsed())
        if not activity:
            return HINT
        budget = self._width - len(HINT) - len(_STATUS_SEP)
        return f"{_elide_to_width(activity, budget)}{_STATUS_SEP}{HINT}"

    def _invalidate(self) -> None:
        self._narrative_text = None
        if self._app is not None:
            self._app.invalidate()

    # -- scroll handlers (read live window metrics) --------------------------
    def _metrics(self) -> tuple[int, int]:
        info = self._body.render_info if self._body is not None else None
        if info is None:
            return (len(self.narrative().split("\n")), 0)
        return (info.ui_content.line_count, info.window_height)

    def scroll_page_up(self) -> None:
        total, height = self._metrics()
        self._viewport.page_up(total_lines=total, height=height)
        self._invalidate()

    def scroll_page_down(self) -> None:
        total, height = self._metrics()
        self._viewport.page_down(total_lines=total, height=height)
        self._invalidate()

    def scroll_up_lines(self, n: int = 3) -> None:
        total, height = self._metrics()
        self._viewport.scroll_up_lines(n, total_lines=total, height=height)
        self._invalidate()

    def scroll_down_lines(self, n: int = 3) -> None:
        total, height = self._metrics()
        self._viewport.scroll_down_lines(n, total_lines=total, height=height)
        self._invalidate()

    def jump_to_bottom(self) -> None:
        self._viewport.jump_to_bottom()
        self._invalidate()

    # -- pause / interrupt (same coordinator as streaming mode) --------------
    async def pause_run(self) -> None:
        await self._coord.signal_pause(reason="esc")

    async def interrupt_run(self) -> None:
        await self._coord.signal_pause(reason="ctrl-c")

    # -- lifecycle / exit transcript -----------------------------------------
    def finish(self) -> None:
        pass

    def print_collapsed_transcript(self, stream: Any) -> None:
        self._driver.print_collapsed_transcript(stream)

    def exit(self) -> None:
        app = self._app
        if app is not None:
            with contextlib.suppress(Exception):
                app.exit()

    # -- prompt_toolkit Application ------------------------------------------
    def _key_bindings(self) -> Any:
        from prompt_toolkit.filters import Condition
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.keys import Keys

        kb = KeyBindings()
        not_prompt = Condition(lambda: not self._prompt_visible)

        # eager=True guarantees these reach the viewport even while the
        # wait_for_user prompt buffer is focused — navigation/verbosity belong to
        # the viewer, not the input line. On today's prompt_toolkit a single-line
        # Buffer happens not to bind PgUp/PgDn and filters emacs `open-line`
        # (Ctrl-O) out, so the global bindings already fire during a solicit (see
        # test_scroll_and_ctrl_o_reach_viewport_during_active_solicit); eager
        # makes that precedence explicit and robust to that incidentally-true
        # behaviour changing.
        @kb.add("pageup", eager=True)
        def _(event: Any) -> None:
            self.scroll_page_up()

        @kb.add("pagedown", eager=True)
        def _(event: Any) -> None:
            self.scroll_page_down()

        # `end` stays prompt-gated: while typing an answer it is the editing
        # end-of-line key (Ctrl-E aside), not jump-to-bottom. PgDn scrolls back
        # down during a solicit, re-engaging follow at the tail.
        @kb.add("end", filter=not_prompt)
        def _(event: Any) -> None:
            self.jump_to_bottom()

        @kb.add("c-o", eager=True)
        def _(event: Any) -> None:
            self.toggle_verbosity()

        @kb.add(Keys.ScrollUp)
        def _(event: Any) -> None:
            self.scroll_up_lines()

        @kb.add(Keys.ScrollDown)
        def _(event: Any) -> None:
            self.scroll_down_lines()

        @kb.add("escape", filter=not_prompt)
        async def _(event: Any) -> None:
            await self.pause_run()

        @kb.add("c-c", filter=not_prompt)
        async def _(event: Any) -> None:
            await self.interrupt_run()

        return kb

    def _vertical_scroll(self, window: Any) -> int:
        info = window.render_info
        if info is None:
            return 0
        return self._viewport.vertical_scroll(
            total_lines=info.ui_content.line_count, height=info.window_height)

    def _narrative_cursor_position(self) -> Any:
        # prompt_toolkit's do_scroll forces vertical_scroll toward the cursor,
        # overriding get_vertical_scroll when cursor is at line 0. Placing the
        # cursor at the last content line makes do_scroll naturally tail-follow
        # (scroll down to show it). When not following, placing the cursor in
        # the middle of the desired viewport prevents do_scroll from overriding
        # the explicit scroll set by get_vertical_scroll.
        from prompt_toolkit.data_structures import Point
        n = len(self.narrative().split("\n"))
        if self._viewport.follow:
            return Point(x=0, y=max(0, n - 1))
        info = self._body.render_info if self._body else None
        height = info.window_height if info else 22  # pre-first-render fallback
        return Point(x=0, y=min(self._viewport.scroll + height // 2, max(0, n - 1)))

    def _build_app(self) -> None:
        from prompt_toolkit.application import Application
        from prompt_toolkit.buffer import Buffer
        from prompt_toolkit.filters import Condition
        from prompt_toolkit.formatted_text import ANSI
        from prompt_toolkit.layout import HSplit, Layout, Window
        from prompt_toolkit.layout.containers import ConditionalContainer
        from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl

        self._body = Window(
            content=FormattedTextControl(
                text=lambda: ANSI(self.narrative()),
                get_cursor_position=self._narrative_cursor_position,
            ),
            get_vertical_scroll=self._vertical_scroll,
            wrap_lines=False, always_hide_cursor=True)
        status = Window(
            content=FormattedTextControl(text=lambda: ANSI(self.status_line())),
            height=1, style="reverse")
        self._buffer = Buffer(
            accept_handler=lambda buf: self._accept(buf),
            multiline=False)
        prompt_win = ConditionalContainer(
            Window(BufferControl(buffer=self._buffer), height=1),
            filter=Condition(lambda: self._prompt_visible))
        slash_win = ConditionalContainer(
            Window(FormattedTextControl(
                text=lambda: ANSI("\n".join(self._slash_lines)))),
            filter=Condition(lambda: self._prompt_visible and bool(self._slash_lines)))
        layout = Layout(
            HSplit([self._body, slash_win, status, prompt_win]),
            focused_element=self._body)
        self._app = Application(
            layout=layout, key_bindings=self._key_bindings(),
            full_screen=True, mouse_support=True)

    async def _refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(0.1)
            self._frame += 1
            self._invalidate()

    async def run_async(self) -> None:
        if self._app is None:
            self._build_app()
        task = asyncio.create_task(self._refresh_loop())
        try:
            # set_exception_handler=False: don't let prompt_toolkit install its
            # loop exception handler, which drops the alt-screen and blocks on a
            # "Press ENTER to continue..." dialog for any loop-level exception. On
            # interrupt the provider SDK's detached tasks fail writing to the
            # killed CLI child — benign teardown that must not wedge the viewer.
            # _run_tui_session installs a pause-gated handler that swallows it.
            await self._app.run_async(
                handle_sigint=False, set_exception_handler=False)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    # -- in-app prompt (wait_for_user) ---------------------------------------
    async def solicit(self, change: Any) -> str:
        self._change = change
        self._slash_lines = []
        self._buffer.completer = SlashCompleter(change.workflow_dir / "skills")
        self._prompt_future = asyncio.get_running_loop().create_future()
        self._prompt_visible = True
        self._app.layout.focus(self._buffer)
        self._invalidate()
        try:
            return cast(str, await self._prompt_future)
        finally:
            self._prompt_visible = False
            self._app.layout.focus(self._body)
            self._invalidate()

    def _resolve_prompt(self, value: str) -> None:
        fut = self._prompt_future
        if fut is not None and not fut.done():
            fut.set_result(value)

    def _accept(self, buffer: Any) -> bool:
        text = buffer.text
        if not is_slash_command(text):
            self._resolve_prompt(text)
            return False
        result = handle_slash_line(
            text, change=self._change, output_fn=self._slash_lines.append)
        if result is REPROMPT:
            self._invalidate()
            return False
        assert isinstance(result, str)
        self._resolve_prompt(result)
        return False
