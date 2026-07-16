"""Pure vi-mode display-row motion core: move cursor by display row (wrapped or hard-newline) rather than logical line."""
from __future__ import annotations

from dataclasses import dataclass

from prompt_toolkit.utils import get_cwidth


@dataclass(frozen=True)
class _Row:
    start: int          # absolute offset of first char in row (in text)
    end: int            # absolute offset past last char (excludes \n)
    last_in_line: bool  # True if last display row of its logical line


def _build_rows(text: str, w: int) -> list[_Row]:
    if not text:
        return [_Row(0, 0, True)]

    rows: list[_Row] = []
    lines = text.split("\n")
    line_start = 0

    for line in lines:
        if not line:
            rows.append(_Row(line_start, line_start, True))
        else:
            row_start = line_start
            running_x = 0
            for i, ch in enumerate(line):
                cw = get_cwidth(ch)
                # running_x > 0 guard: a char wider than w still gets its own row
                if running_x > 0 and running_x + cw > w:
                    rows.append(_Row(row_start, line_start + i, False))
                    row_start = line_start + i
                    running_x = 0
                running_x += cw
            rows.append(_Row(row_start, line_start + len(line), True))
        line_start += len(line) + 1

    return rows


def _find_row(rows: list[_Row], cursor: int) -> int:
    for i, row in enumerate(rows):
        if row.last_in_line:
            if row.start <= cursor <= row.end:
                return i
        else:
            if row.start <= cursor < row.end:
                return i
    return len(rows) - 1


def _cursor_at_x(text: str, row: _Row, target_x: int) -> int:
    """Return offset of the insertion point whose visual x is the greatest <= target_x."""
    x = 0
    pos = row.start
    while pos < row.end:
        cw = get_cwidth(text[pos])
        if x + cw > target_x:
            break
        x += cw
        pos += 1
    # For soft-wrap rows the exclusive end belongs to the next row visually, so
    # clamp to the last character of this row. Guard against empty rows (start==end).
    if not row.last_in_line and pos == row.end and row.end > row.start:
        pos = row.end - 1
    return pos


def display_line_move(
    text: str,
    cursor: int,
    *,
    w: int,
    count: int,
    direction: int,
    preferred_x: int | None,
) -> tuple[int, int]:
    """Move cursor by `count` display rows in `direction` (+1 down, -1 up).

    Returns (new_cursor, preferred_x); preferred_x=None computes and returns the
    current visual x, non-None passes through unchanged.
    """
    rows = _build_rows(text, w)
    current_idx = _find_row(rows, cursor)
    current_row = rows[current_idx]

    current_x = sum(get_cwidth(text[p]) for p in range(current_row.start, cursor))
    target_x = preferred_x if preferred_x is not None else current_x

    target_idx = max(0, min(len(rows) - 1, current_idx + direction * count))
    new_cursor = _cursor_at_x(text, rows[target_idx], target_x)

    return new_cursor, target_x
