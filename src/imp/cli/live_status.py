# src/imp/cli/live_status.py
"""The single mutable bottom status line for the live run view.

TTY-only: on a non-TTY stream (pipe / CI) every method is inert, so output is
just the permanent folded blocks the driver prints (design Part 3). Exactly one
line is ever rewritten — `\\r` + erase-to-EOL — never an alternate screen.

LiveStatus knows nothing about NarrativeBuilder: the driver supplies a `compose`
callback `(frame: int, elapsed: float) -> str | None` that renders the current
line. compose returning None means "nothing to show" (the line is erased)."""
from __future__ import annotations

import contextlib
import shutil
import time
from collections.abc import Callable, Iterator
from typing import Any

from imp.cli import palette as pal

_ERASE = "\r\x1b[K"     # carriage return + clear-to-end-of-line
_DIM = "\x1b[2m"        # SGR 2 (dim)
_RESET_SGR = "\x1b[0m"  # SGR 0 (reset all attributes)
_ACK_MAX_WIDTH = 120    # clip the transient ack line so it never wraps


def term_width() -> int:
    try:
        return shutil.get_terminal_size().columns
    except OSError:
        return 80


def _display_width(text: str) -> int:
    """Escape-aware display column count for narrowing detection."""
    return pal.display_width(text)


class LiveStatus:
    def __init__(self, stream: Any, *, now: Callable[[], float] = time.monotonic) -> None:
        self._stream = stream
        self._enabled = bool(getattr(stream, "isatty", lambda: False)())
        self._now = now
        self._compose: Callable[[int, float], str | None] | None = None
        self._session_start: float | None = None
        self._visible = False
        self._suspended = False
        self._frame = 0
        self._prev_width: int | None = None     # display width of the last drawn frame
        self._width_fn: Callable[[], int] = term_width

    def update(self, compose: Callable[[int, float], str | None]) -> None:
        """Set/refresh the compose fn and redraw. Starts the session timer on the
        first call after a reset, and keeps it across hide()/update() cycles so
        `elapsed` grows monotonically rather than resetting per-event. In practice
        the driver's `current_activity()` stays non-None from the first phase to
        the end of the run (it falls back to the phase name between steps), so the
        timer effectively measures elapsed run time until finish()."""
        self._compose = compose
        if self._session_start is None:
            self._session_start = self._now()
        self._draw()

    def hide(self) -> None:
        """Erase the on-screen line but KEEP the session timer — used before
        printing a permanent block; the next update() re-shows with the same
        elapsed."""
        self._erase()

    def reset(self) -> None:
        """Erase AND end the session, restarting the timer on the next update().
        Driven by the driver's `_refresh_status` when `current_activity()` is None
        (an idle gap with no active phase — in practice only before the first
        phase and after the run ends, which is when `finish()` calls it)."""
        self._erase()
        self._compose = None
        self._session_start = None

    def restart_span(self) -> None:
        """Reset the elapsed timer to zero for a new activity span (§7). Unlike
        reset(), this keeps the compose fn — the line keeps drawing, just from a
        fresh t0."""
        self._session_start = self._now()
        self._draw()

    def elapsed(self) -> float:
        """Current span elapsed in seconds; 0.0 before the timer starts."""
        if self._session_start is None:
            return 0.0
        return self._now() - self._session_start

    async def tick(self, interval: float = 0.1) -> None:
        """Background spinner: advance the frame and redraw on an interval so the
        line animates during long LLM/tool waits with no events."""
        import anyio
        while True:
            await anyio.sleep(interval)
            self._frame += 1
            self._draw()

    @contextlib.contextmanager
    def suspend(self) -> Iterator[None]:
        """Clear the line and SUPPRESS drawing around terminal I/O (a prompt or a
        banner). Setting `_suspended` is load-bearing: the concurrent `tick` task
        (started via tg.start_soon(status.tick)) keeps firing `_draw()` every
        interval while `prompt.ask_async` awaits keystrokes; without this guard it
        would write the spinner line over the user's input.

        On exit the span timer is RESET (§7): the suspend block only ever wraps the
        interactive solicit loop, so the suspended interval was a human at the
        prompt — the agent was not working. A wait_for_user reply returns as a
        tool_result, not a user_message, so the driver's restart_span never fires
        for it; resetting here is what makes the resumed `thinking` line count from
        the reply rather than from before the prompt (which would fold in the
        human's read/type time — the `imp new` first-turn "thinking 26s" bug). An
        unstarted timer is left unstarted (update() owns first-start)."""
        self._suspended = True
        self._erase()
        try:
            yield
        finally:
            self._suspended = False
            if self._session_start is not None:
                self._session_start = self._now()
            self._draw()

    def show_stop_ack(self, text: str) -> None:
        """Render a transient dim stop-acknowledgment line in the status-line
        area (console-only chrome: width-clipped, erased in place, never
        journaled). Uses the same `_ERASE` + stream-write path as the normal
        status line draw. No-op on non-TTY or while suspended (a prompt owns the
        tty then — writing would corrupt it, matching `_draw`'s discipline).
        Called by the ack hook wired in E4 via
        `coord.set_ack_hook(status.show_stop_ack)`."""
        if not self._enabled or self._suspended:
            return
        if len(text) > _ACK_MAX_WIDTH:
            text = text[:_ACK_MAX_WIDTH]
        # The _ERASE at the start also erases any prior status line.
        self._stream.write(f"{_ERASE}{_DIM}{text}{_RESET_SGR}")
        self._stream.flush()
        self._visible = True

    # -- internals ------------------------------------------------------------
    def _draw(self) -> None:
        if not self._enabled or self._compose is None or self._suspended:
            return
        start = self._session_start if self._session_start is not None else self._now()
        elapsed = self._now() - start
        text = self._compose(self._frame, elapsed)
        if text is None:
            self._erase()
            return
        width = self._width_fn()
        # Clip to width-1 (§5b): a status that fills the whole width can
        # auto-wrap and resurrect committed-spinner artifact.
        clipped = pal.clip_display(text, width - 1)
        new_w = _display_width(clipped)
        if self._visible and self._prev_width is not None and (width - 1) < self._prev_width:
            # Narrowing transition: reflow-safe abandon. Erase current row only,
            # do NOT cursor-up, mark hidden, redraw fresh below.
            self._stream.write(_ERASE)
            self._visible = False
            self._prev_width = None
        if self._visible:
            # Already showing: rewrite in place on the same row.
            self._stream.write(f"{_ERASE}{clipped}")
        else:
            # First draw after hidden: own the blank line, then status below it.
            self._stream.write(f"\n{clipped}")
        self._stream.flush()
        self._visible = True
        self._prev_width = new_w

    def _erase(self) -> None:
        if self._enabled and self._visible:
            # Erase the status text, then climb onto the status-owned blank so
            # the next printed block overwrites it. The cursor-up lives INSIDE the
            # visibility guard — from the not-visible state this is a strict no-op.
            self._stream.write(_ERASE + "\x1b[1A")
            self._stream.flush()
        self._visible = False
        self._prev_width = None
