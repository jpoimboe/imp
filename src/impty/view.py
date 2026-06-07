"""Pure rendering helpers over a pyte screen. No pty/process/IO here, so these
are trivially unit-testable by feeding bytes to a screen."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from impty.screen import Regime


@dataclass(frozen=True)
class Cell:
    """One rendered character cell: the glyph plus its display attributes."""

    data: str
    fg: str
    bg: str
    bold: bool
    italics: bool
    underscore: bool
    reverse: bool


def viewport_text(screen: Any) -> str:
    """The current visible grid as text — what a human sees right now.
    Each row is right-trimmed; internal blank rows are preserved (layout)."""
    return "\n".join(line.rstrip() for line in screen.display)


def cursor_pos(screen: Any) -> tuple[int, int]:
    """(x, y) cursor position in the viewport. Parked just after a prompt
    string is the cheap 'waiting for input' tell."""
    return (screen.cursor.x, screen.cursor.y)


def cell_at(screen: Any, x: int, y: int) -> Cell:
    """The Cell at viewport column x, row y — glyph + style attributes."""
    ch = screen.buffer[y][x]
    return Cell(
        data=ch.data,
        fg=ch.fg,
        bg=ch.bg,
        bold=bool(ch.bold),
        italics=bool(ch.italics),
        underscore=bool(ch.underscore),
        reverse=bool(ch.reverse),
    )


def regime(screen: Any) -> Regime:
    """Which shape the session is in. The single source of truth is
    screen.regime (a TerminalScreen attribute)."""
    return cast(Regime, screen.regime)


def is_alt_screen(screen: Any) -> bool:
    """True if the program is on the alternate screen (a full TUI like vim).
    Derives from screen.regime, so it can't drift from the buffer swap."""
    return screen.regime is Regime.SCREEN


def _row_text(row: Any, cols: int) -> str:
    return "".join(row[x].data if x in row else " " for x in range(cols)).rstrip()


def transcript_text(screen: Any, limit: int | None = None) -> str:
    """The whole run in order: scrolled-off history + the current viewport.
    For an append-only CLI this is the full scroll. `limit` keeps only the
    last N non-trailing-blank lines (bounded by construction — never an
    unbounded dump)."""
    cols = screen.columns
    history = [_row_text(row, cols) for row in screen.history.top]
    viewport = [line.rstrip() for line in screen.display]
    ls = history + viewport
    while ls and not ls[-1]:        # drop viewport bottom padding
        ls.pop()
    if limit is not None:
        ls = ls[-limit:]
    return "\n".join(ls)


def lines(screen: Any, limit: int | None = None) -> list[str]:
    """Rendered text rows, top->bottom, over `scrollback_rows + screen_rows`.

    The screen part is ALWAYS exactly `rows` entries (blank rows preserved,
    each rstripped internally) — today's viewport_text semantics. In SCREEN
    regime (alt) there is no scrollback; the strip is just the screen.

    With a `limit` (the windowed read): the last N entries with NO trailing-
    blank stripping, so lines(limit=rows) is exactly the live screen and
    lines(limit=rows)[y] is cell(., y)'s row — the alignment invariant.

    With no limit (the full strip): the whole sequence, right-trimmed of
    trailing blank rows for readability. No alignment promise on this form.
    """
    screen_rows = [line.rstrip() for line in screen.display]  # exactly `rows`
    if screen.regime is Regime.SCREEN:
        seq = screen_rows
    else:
        cols = screen.columns
        history = [_row_text(row, cols) for row in screen.history.top]
        seq = history + screen_rows
    if limit is not None:
        return seq[-limit:]                  # windowed: NO trailing-blank trim
    out = list(seq)
    while out and not out[-1]:               # unbounded: trim trailing blanks
        out.pop()
    return out
