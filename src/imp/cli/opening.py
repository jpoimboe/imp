from __future__ import annotations

from imp.cli import palette as pal

# 6-row source: "X"=pixel, "."=transparent; rows pair (0,1)(2,3)(4,5) into 3 half-block cells.
_WORDMARK_BITMAP: tuple[str, ...] = (
    "X",
    ".",
    "X.XXXXX.XXX",
    "X.X.X.X.X.X",
    "X.X.X.X.XXX",
    "........X..",
)

# (top_pixel, bottom_pixel) → cell glyph. Never paints a background cell.
_HALF_BLOCKS: dict[tuple[bool, bool], str] = {
    (True, True): "█",
    (True, False): "▀",
    (False, True): "▄",
    (False, False): " ",
}


def _half_block_rows() -> list[str]:
    width = max(len(row) for row in _WORDMARK_BITMAP)
    rows: list[str] = []
    for top_idx in range(0, len(_WORDMARK_BITMAP), 2):
        top = _WORDMARK_BITMAP[top_idx]
        bottom = _WORDMARK_BITMAP[top_idx + 1]
        cells = [
            _HALF_BLOCKS[
                (col < len(top) and top[col] == "X",
                 col < len(bottom) and bottom[col] == "X")
            ]
            for col in range(width)
        ]
        rows.append("".join(cells).rstrip())
    return rows


def render_wordmark(*, theme: str, color: bool, unicode: bool = True) -> str:
    """The `imp` wordmark: 3-row half-block art, else the plain bold word `imp`."""
    esc = pal.theme_escapes(theme)
    if not unicode:
        return f"{esc['bold']}{esc['wordmark']}imp{esc['reset']}" if color else "imp"
    lines = _half_block_rows()
    if color:
        return "\n".join(f"{esc['wordmark']}{line}{esc['reset']}" for line in lines)
    return "\n".join(lines)
