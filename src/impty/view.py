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
