"""Pure rendering helpers over a pyte screen. No pty/process/IO here, so these
are trivially unit-testable by feeding bytes to a screen."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


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


# pyte stores private modes left-shifted by 5; alt-screen 1049 -> 1049<<5.
_ALT_SCREEN_MODES = frozenset((1049 << 5, 1047 << 5, 47 << 5))


def is_alt_screen(screen: Any) -> bool:
    """True if the program has switched to the alternate screen buffer
    (a TUI like claude code). Detection only — buffer-swap fidelity is a
    later slice. For a plain CLI like imp this is always False."""
    return bool(_ALT_SCREEN_MODES & set(screen.mode))


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
    lines = history + viewport
    while lines and not lines[-1]:        # drop viewport bottom padding
        lines.pop()
    if limit is not None:
        lines = lines[-limit:]
    return "\n".join(lines)
