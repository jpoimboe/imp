from __future__ import annotations

import asyncio  # noqa: F401
import contextlib
import time
from typing import Any

from imp.cli.prompt import SlashCompleter  # noqa: F401
from imp.cli.slash import REPROMPT, handle_slash_line, is_slash_command  # noqa: F401
from imp.cli.tui_driver import TuiDriver
from imp.cli.tui_viewport import ViewportState

HINT = "PgUp/PgDn scroll · End follow · ^O verbosity · ESC pause · ^C interrupt"


class TuiApp:
    def __init__(self, *, coord: Any, color: bool, width: int) -> None:
        self._coord = coord
        self._color = color
        self._width = width
        self._full = True
        self._frame = 0
        self._start = time.monotonic()
        self._viewport = ViewportState()
        self._driver = TuiDriver(
            color=color, width=width, on_invalidate=self._invalidate)
        self._app: Any = None        # prompt_toolkit Application (Task 3)
        self._body: Any = None       # the narrative Window (for render_info metrics)
        self._buffer: Any = None     # the bottom prompt Buffer (Task 4)
        self._change: Any = None     # set per-solicit, for slash dispatch
        self._prompt_visible = False
        self._prompt_future: Any = None
        self._slash_lines: list[str] = []

    # -- event intake ---------------------------------------------------------
    def on_event(self, event: Any) -> None:
        self._driver.on_event(event)

    # -- narrative / status / verbosity --------------------------------------
    def narrative(self) -> str:
        return self._driver.render(full=self._full)

    def toggle_verbosity(self) -> None:
        self._full = not self._full
        self._invalidate()

    def _elapsed(self) -> float:
        return time.monotonic() - self._start

    def status_line(self) -> str:
        activity = self._driver.status_text(self._frame, self._elapsed())
        return f"{activity}   {HINT}" if activity else HINT

    def _invalidate(self) -> None:
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

        @kb.add("pageup")
        def _(event: Any) -> None:
            self.scroll_page_up()

        @kb.add("pagedown")
        def _(event: Any) -> None:
            self.scroll_page_down()

        @kb.add("end", filter=not_prompt)
        def _(event: Any) -> None:
            self.jump_to_bottom()

        @kb.add("c-o")
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

    def _build_app(self) -> None:
        from prompt_toolkit.application import Application
        from prompt_toolkit.buffer import Buffer
        from prompt_toolkit.filters import Condition
        from prompt_toolkit.formatted_text import ANSI
        from prompt_toolkit.layout import HSplit, Layout, Window
        from prompt_toolkit.layout.containers import ConditionalContainer
        from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl

        self._body = Window(
            content=FormattedTextControl(text=lambda: ANSI(self.narrative())),
            get_vertical_scroll=self._vertical_scroll,
            wrap_lines=False, always_hide_cursor=True)
        status = Window(
            content=FormattedTextControl(text=lambda: ANSI(self.status_line())),
            height=1, style="reverse")
        self._buffer = Buffer(
            accept_handler=lambda buf: self._accept(buf),  # type: ignore[attr-defined]
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

    async def run_async(self) -> None:
        if self._app is None:
            self._build_app()
        await self._app.run_async(handle_sigint=False)
