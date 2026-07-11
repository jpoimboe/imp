# src/imp/cli/live_status.py
"""Fixed multi-row footer block for the live run view.

TTY-only: on a non-TTY stream (pipe / CI) every method is inert, so output is
just the permanent folded blocks the driver prints (design Part 3). The footer
owns one blank line above the block (the status-owned blank); _erase() always
lands the cursor on that blank so the next permanent print overwrites it.

LiveStatus knows nothing about NarrativeBuilder: the driver supplies a `compose`
callback `(frame: int, elapsed: float) -> list[str] | None` that renders the
footer rows (top→bottom). compose returning None means "nothing to show"."""
from __future__ import annotations

import atexit
import contextlib
import shutil
import time
from collections.abc import Callable, Iterator
from typing import Any

from imp.cli import palette as pal

_ERASE = "\r\x1b[K"     # carriage return + clear-to-end-of-line
_UP = "\x1b[1A"         # cursor up one row
_DIM = "\x1b[2m"        # SGR 2 (dim)
_RESET_SGR = "\x1b[0m"  # SGR 0 (reset all attributes)
_ACK_MAX_WIDTH = 120    # clip the transient ack line so it never wraps
_HIDE = "\x1b[?25l"     # DECTCEM: hide cursor
_SHOW = "\x1b[?25h"     # DECTCEM: show cursor


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
        self._cursor_hidden = False
        if self._enabled:
            atexit.register(self._show_cursor)
        self._now = now
        self._compose: Callable[[int, float], list[str] | None] | None = None
        self._session_start: float | None = None
        self._visible = False
        self._suspended = False
        self._frame = 0
        self._prev_width: int | None = None     # display width of the last drawn frame
        self._width_fn: Callable[[], int] = term_width
        self._rows = 0                          # number of footer rows currently on screen
        self._drawn_widths: list[int] = []      # display width of each on-screen row
        self._owns_lead_blank = True     # a footer normally owns one blank directly above it
        self._skip_lead_once = False     # one-shot: next first-draw owns NO leading blank

    def update(self, compose: Callable[[int, float], list[str] | None]) -> None:
        """Set/refresh the compose fn and redraw. Starts the session timer on the
        first call after a reset, and keeps it across hide()/update() cycles so
        `elapsed` grows monotonically rather than resetting per-event."""
        self._compose = compose
        if self._session_start is None:
            self._session_start = self._now()
        self._draw()

    def hide(self) -> None:
        """Erase the footer block but KEEP the session timer."""
        self._erase()

    def reset(self) -> None:
        """Erase AND end the session, restarting the timer on the next update()."""
        self._erase()
        self._compose = None
        self._session_start = None

    def restart_span(self) -> None:
        """Reset the elapsed timer to zero for a new activity span."""
        self._session_start = self._now()
        self._draw()

    def elapsed(self) -> float:
        """Current span elapsed in seconds; 0.0 before the timer starts."""
        if self._session_start is None:
            return 0.0
        return self._now() - self._session_start

    async def tick(self, interval: float = 0.1) -> None:
        """Background spinner: advance the frame and redraw on an interval."""
        import anyio
        while True:
            await anyio.sleep(interval)
            self._frame += 1
            self._draw()

    @contextlib.contextmanager
    def suspend(self) -> Iterator[None]:
        """Clear the footer and suppress drawing around terminal I/O (a prompt).

        On exit the span timer is reset: the suspended interval was a human at the
        prompt — the agent was not working. An unstarted timer is left unstarted."""
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
        """Render a transient dim stop-acknowledgment line in the status area.

        No-op on non-TTY or while suspended."""
        if not self._enabled or self._suspended:
            return
        if len(text) > _ACK_MAX_WIDTH:
            text = text[:_ACK_MAX_WIDTH]
        # Erase the WHOLE current footer block first (it may be multi-row) before drawing
        # the single-row ack, so the upper rows never leak into scroll-back. _erase() lands
        # the cursor on the status-owned blank and is a strict no-op when nothing is visible.
        was_visible = self._visible
        owned_blank = self._owns_lead_blank
        self._erase()
        # When an owned-blank block was erased the cursor now sits ON the owned blank; draw
        # the ack one row BELOW it (lead "\n") so the blank is preserved and the next _erase
        # climbs back onto it — not one row too high onto committed scroll-back. On a fresh
        # status (_erase was a no-op) or a skip-drawn footer (owns no blank, _erase cleared
        # in place) draw in place, identical to the single-line path.
        lead = "\n" if (was_visible and owned_blank) else ""
        self._hide_cursor()
        self._stream.write(f"{lead}{_ERASE}{_DIM}{text}{_RESET_SGR}")
        self._stream.flush()
        self._visible = True
        self._rows = 1
        self._drawn_widths = [_display_width(text)]

    def skip_leading_blank(self) -> None:
        """Prime the next first-draw to own NO leading blank: a committed blank
        already sits above the install point (a splash tagline or an inert
        question). Pairs with _erase, which then clears the footer in place
        instead of climbing onto that committed blank."""
        self._skip_lead_once = True

    # -- internals ------------------------------------------------------------
    def _hide_cursor(self) -> None:
        if self._enabled and not self._cursor_hidden:
            self._stream.write(_HIDE)
            self._cursor_hidden = True

    def _show_cursor(self) -> None:
        if self._cursor_hidden:
            self._stream.write(_SHOW)
            self._stream.flush()
            self._cursor_hidden = False

    def _physical_rows(self) -> int:
        """Physical line count of the on-screen footer at the CURRENT width.

        Each row was clipped to its draw-time width, so at a stable width it is
        one physical line. But a terminal that has since NARROWED soft-wraps any
        row wider than the new width into ceil(display_width / width) lines (an
        empty row still occupies one). Erase/abandon must climb this reflowed
        count, not the logical row count — otherwise the wrapped tails survive as
        a duplicated footer in scroll-back (the resize-repeat bug). At an
        unchanged or widened width every row still fits, so this equals _rows."""
        if not self._drawn_widths:
            return self._rows
        w = self._width_fn()
        if w <= 0:
            return self._rows
        return sum(1 if dw <= 0 else -(-dw // w) for dw in self._drawn_widths)

    def _draw(self) -> None:
        if not self._enabled or self._compose is None or self._suspended:
            return
        start = self._session_start if self._session_start is not None else self._now()
        elapsed = self._now() - start
        block = self._compose(self._frame, elapsed)
        if not block:
            self._erase()
            return
        width = self._width_fn()
        rows = [pal.clip_display(r, width - 1) for r in block]
        widths = [_display_width(r) for r in rows]
        new_w = max(widths, default=0)
        if (self._visible and self._prev_width is not None
                and (width - 1) < self._prev_width):
            # Narrowing: the committed rows have soft-wrapped to more physical lines
            # than they were drawn with. Fully erase them — climbing the reflowed
            # physical-line count onto the owned blank — so the redraw below reuses the
            # SAME position. Clearing in place and redrawing lower would instead strand
            # the surplus lines as a blank gap between the narrative and the footer.
            # _erase() resets _visible / _prev_width / _drawn_widths itself.
            self._erase()
        if self._visible and self._rows == len(rows):
            self._hide_cursor()
            self._stream.write("\r")
            if self._rows > 1:
                self._stream.write(f"\x1b[{self._rows - 1}A")
            self._stream.write("\n".join(_ERASE + r for r in rows))
        else:
            if self._visible:
                self._erase()
            self._hide_cursor()
            skip = self._skip_lead_once
            self._skip_lead_once = False
            self._owns_lead_blank = not skip
            lead = "" if skip else "\n"
            self._stream.write(lead + "\n".join(_ERASE + r for r in rows))
        self._stream.flush()
        self._visible = True
        self._prev_width = new_w
        self._rows = len(rows)
        self._drawn_widths = widths

    def _erase(self) -> None:
        if self._enabled and self._visible:
            # From the end of the last row, clear each PHYSICAL line bottom-up and climb
            # one line per physical line, ending on the status-owned blank above the
            # block. Using the reflowed physical count (not the logical row count) keeps
            # erase correct after the terminal has narrowed and the rows soft-wrapped.
            phys = self._physical_rows()
            self._stream.write("\r")
            for i in range(phys):
                self._stream.write("\x1b[K")
                if self._owns_lead_blank or i < phys - 1:
                    self._stream.write(_UP)
            self._show_cursor()
            self._stream.flush()
        self._visible = False
        self._prev_width = None
        self._rows = 0
        self._drawn_widths = []
