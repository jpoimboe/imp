"""TerminalScreen — a pyte HistoryScreen that implements the alternate screen
and is the single source of truth for which *regime* the session is in.

pyte records the alt-screen mode bit but never swaps buffers, so alt-screen
content bleeds into scrollback and the pre-alt screen is never restored. This
subclass intercepts set_mode/reset_mode to do a real two-buffer swap, suppresses
history while in alt (so "alt has no scrollback" holds at the buffer level), and
hand-rolls resize-during-alt on the detached saved buffer. It also absorbs the
old _ReplyScreen CPR/DA shim so prompt_toolkit/curses apps still unblock.
"""

from __future__ import annotations

import copy
import enum
from collections import defaultdict
from collections.abc import Callable
from typing import Any

import pyte
from pyte.screens import Char, Cursor, Margins, StaticDefaultDict


class Regime(enum.Enum):
    """The shape the session is in. SCROLL: an unbounded strip (append-only or
    inline-redraw) — a live rows x cols screen with frozen scrollback above.
    SCREEN: alt-screen — content IS exactly rows x cols, no scrollback."""

    SCROLL = "scroll"
    SCREEN = "screen"


# Private mode codes that switch to/from the alternate screen. Membership-tested
# against the RAW values pyte passes set_mode/reset_mode (pyte shifts <<5 itself).
# All three are treated identically (save+restore the cursor; discard the alt
# buffer on exit). This is a deliberate superset of what 47/1047 strictly
# specify (they don't save the cursor; 1047 clears the alt buffer before exit) —
# harmless for rendering fidelity and simpler than per-mode behavior.
_ALT_MODES = frozenset((1049, 1047, 47))


class TerminalScreen(pyte.HistoryScreen):
    """A HistoryScreen with a real alternate-screen buffer swap.

    `regime` is authoritative; `view.is_alt_screen` derives from it rather than
    re-reading the raw mode bit, so the two can't drift.
    """

    # Set by TerminalSession after the pty opens; forwards pyte-computed query
    # replies (CPR / device attributes) to the program's stdin like a real tty.
    reply: Callable[[str], None] | None = None
    # Class-level default so the index/reverse_index overrides are safe even if
    # they were ever reached before __init__ finishes (reset() does not call
    # them, but this is cheap defense).
    regime: Regime = Regime.SCROLL

    def __init__(
        self, columns: int, lines: int, history: int = 100, ratio: float = 0.5
    ) -> None:
        super().__init__(columns, lines, history=history, ratio=ratio)
        self.regime = Regime.SCROLL
        self._saved_buffer: dict[int, StaticDefaultDict[int, Char]] | None = None
        self._saved_cursor: Cursor | None = None
        self._saved_margins: Margins | None = None
        self._saved_lines: int = 0
        self._saved_columns: int = 0

    # --- CPR / device-attribute reply shim (absorbed from _ReplyScreen) ----
    def write_process_input(self, data: str) -> None:
        if self.reply is not None:
            self.reply(data)

    # --- alt-screen entry / exit ------------------------------------------
    def set_mode(self, *modes: int, **kwargs: Any) -> None:
        if kwargs.get("private") and _ALT_MODES & set(modes):
            self._enter_alt()
        super().set_mode(*modes, **kwargs)

    def reset_mode(self, *modes: int, **kwargs: Any) -> None:
        if kwargs.get("private") and _ALT_MODES & set(modes):
            self._exit_alt()
        super().reset_mode(*modes, **kwargs)

    def _fresh_grid(self) -> defaultdict[int, StaticDefaultDict[int, Char]]:
        # Rebind a brand-new grid exactly as Screen.__init__ builds it. NOT
        # reset()/buffer.clear() — that mutates in place and would clobber the
        # saved reference.
        return defaultdict(lambda: StaticDefaultDict(self.default_char))

    def _enter_alt(self) -> None:
        if self.regime is Regime.SCREEN:
            return  # double-enter guard: don't overwrite the saved buffer
        self._saved_buffer = self.buffer
        self._saved_cursor = copy.copy(self.cursor)  # pyte mutates cursor in place
        self._saved_margins = self.margins
        self._saved_lines = self.lines
        self._saved_columns = self.columns
        self.buffer = self._fresh_grid()
        self.margins = None       # default full-screen margins (DECSTBM reset)
        self.cursor.x = 0         # home the cursor on the clean alt screen
        self.cursor.y = 0
        self.regime = Regime.SCREEN

    def _exit_alt(self) -> None:
        if self.regime is Regime.SCROLL or self._saved_buffer is None:
            return  # spurious-exit guard: nothing saved -> don't restore None
        self.buffer = self._saved_buffer
        if self._saved_cursor is not None:
            self.cursor = self._saved_cursor
        self.margins = self._saved_margins
        self.regime = Regime.SCROLL
        self._saved_buffer = None
        self._saved_cursor = None
        self._saved_margins = None

    # --- history suppression while in alt ---------------------------------
    def index(self) -> None:
        if self.regime is Regime.SCREEN:
            pyte.Screen.index(self)        # scroll WITHOUT history.top append
        else:
            super().index()                # HistoryScreen: append then scroll

    def reverse_index(self) -> None:
        if self.regime is Regime.SCREEN:
            pyte.Screen.reverse_index(self)  # scroll WITHOUT history.bottom append
        else:
            super().reverse_index()

    # --- resize (incl. the detached saved buffer while in alt) ------------
    def resize(self, lines: int | None = None, columns: int | None = None) -> None:
        super().resize(lines=lines, columns=columns)  # resizes the LIVE buffer
        if self.regime is Regime.SCREEN and self._saved_buffer is not None:
            self._resize_saved(self.lines, self.columns)

    def _resize_saved(self, new_lines: int, new_cols: int) -> None:
        """Hand-roll pyte's pad/clip/delete on the DETACHED saved buffer (pyte's
        resize only touches self.buffer). Also clamp the saved cursor so an
        un-clamped restore can't put cursor.y/x past the new bounds.

        (super().resize() already reset the LIVE margins via set_margins(); we
        leave _saved_margins as captured. _saved_margins is NOT clamped: a
        session with custom DECSTBM scroll margins that shrinks below them mid-
        alt could restore out-of-bounds margins on exit — a rare edge accepted
        as out of scope, since custom margins in scroll/inline regime are
        unusual and resize-during-alt is itself an edge case.)"""
        buf = self._saved_buffer
        assert buf is not None
        if new_cols < self._saved_columns:                 # clip columns at right
            for row in buf.values():
                for x in range(new_cols, self._saved_columns):
                    row.pop(x, None)
        if new_lines < self._saved_lines:                  # drop rows from the top
            offset = self._saved_lines - new_lines
            rebased = self._fresh_grid()
            for y in range(new_lines):
                if (y + offset) in buf:
                    rebased[y] = buf[y + offset]
            self._saved_buffer = buf = rebased
        # Row/column GROWTH needs no work: the defaultdict/StaticDefaultDict pad
        # missing rows/columns with the default char on read.
        self._saved_lines = new_lines
        self._saved_columns = new_cols
        if self._saved_cursor is not None:
            self._saved_cursor.x = min(self._saved_cursor.x, new_cols - 1)
            self._saved_cursor.y = min(self._saved_cursor.y, new_lines - 1)
