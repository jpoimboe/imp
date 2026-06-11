"""Render an ANSI text blob (read from a file) into a color PNG via impty.

Usage:
    PYTHONPATH=src python3 render_png.py <ansi_file> <out_png> [theme] [cols]

`theme` defaults to "dark". `cols` defaults to auto (longest visible line).
This is the impty render path (pyte screen -> pillow PNG), the same pipeline
scripts/snap_imp_ui.py uses, lifted into a standalone tool so it can render any
captured ANSI (imp status, the resume picker body, a pause line, a transcript
slice) without going through the two built-in scenes.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pyte

from impty.render import render_screen, show_faint


def _imp_theme(theme: str) -> dict:
    from imp.cli import palette as pal

    bg, fg = pal.surface(theme)
    rgb = pal.theme_rgb(theme)
    palette = {
        "red": rgb["red"], "green": rgb["green"],
        "yellow": rgb["yellow"], "brown": rgb["yellow"],
        "blue": rgb["blue"], "magenta": rgb["magenta"], "cyan": rgb["cyan"],
        "brightblack": rgb["gray"], "brightblue": rgb["bright_blue"],
        "black": bg, "white": fg, "brightwhite": fg,
    }
    faint = "\x1b[38;2;{};{};{}m".format(*rgb["gray"])
    return {"bg": bg, "fg": fg, "palette": palette, "faint": faint}


_VISIBLE = re.compile(r"\x1b\[[0-9;]*m")


def _visible_len(line: str) -> int:
    return len(_VISIBLE.sub("", line))


def main() -> None:
    ansi_file = Path(sys.argv[1])
    out_png = sys.argv[2]
    theme = sys.argv[3] if len(sys.argv) > 3 else "dark"
    ansi = ansi_file.read_text()

    th = _imp_theme(theme)
    lines = ansi.rstrip("\n").split("\n")
    cols = int(sys.argv[4]) if len(sys.argv) > 4 else max(
        (_visible_len(ln) for ln in lines), default=20) + 2
    rows = max(1, len(lines)) + 1  # +1 so the trailing newline never scrolls off line 0

    screen = pyte.Screen(cols, rows)
    pyte.Stream(screen).feed(
        show_faint(ansi, th["faint"]).replace("\n", "\r\n"))
    render_screen(screen, out_png, bg=th["bg"], fg=th["fg"],
                  palette=th["palette"], full=False)
    print(out_png)


if __name__ == "__main__":
    main()
