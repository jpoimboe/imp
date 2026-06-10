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
import time
from collections.abc import Callable, Iterator
from typing import Any

_ERASE = "\r\x1b[K"     # carriage return + clear-to-end-of-line
_DIM = "\x1b[2m"        # SGR 2 (dim)
_RESET_SGR = "\x1b[0m"  # SGR 0 (reset all attributes)
_ACK_MAX_WIDTH = 120    # clip the transient ack line so it never wraps


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
        would write the spinner line over the user's input. The timer is preserved;
        on exit drawing resumes and the line is re-shown."""
        self._suspended = True
        self._erase()
        try:
            yield
        finally:
            self._suspended = False
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
        self._stream.write(f"{_ERASE}{text}")
        self._stream.flush()
        self._visible = True

    def _erase(self) -> None:
        if self._enabled and self._visible:
            self._stream.write(_ERASE)
            self._stream.flush()
        self._visible = False
