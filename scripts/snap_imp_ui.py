#!/usr/bin/env python
"""Render an imp UI scene to a PNG you can open — the human-facing companion to
the agent-driven impty harness.

`capture_imp_ui.py` plays a scene to a pty (for an agent to snapshot); this
drives it and writes an image, so you can SEE imp's TUI from the stored fixture
data without running a live agent.

Run from the repo root with the project venv (it has pyte + pillow):

    .venv/bin/python scripts/snap_imp_ui.py stream --open
    .venv/bin/python scripts/snap_imp_ui.py stream --limit 120 --out /tmp/head.png --open
    .venv/bin/python scripts/snap_imp_ui.py prompt --type "rename the helper" --open

Scenes:
  stream   Replay the recorded run journal (tests/fixtures/...jsonl.gz) through
           the real LiveDriver. Rendered in-process — no pty, no agent, no cost.
           --limit N ends the slice on a chosen state (small N = header).
  prompt   The real interactive input field (prompt_toolkit), driven on a pty.
           --type TEXT pre-fills the field before the snapshot.

--out defaults to /tmp/imp_ui/<scene>.png. --open launches xdg-open.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))  # import the sibling scene module by name

import pyte  # noqa: E402  (after sys.path tweak)

from impty.render import render_screen  # noqa: E402

_OUT_DIR = Path("/tmp/imp_ui")


def _feed_screen(ansi: str, *, cols: int, rows: int) -> pyte.Screen:
    """A pyte screen with `ansi` drawn into it. LiveDriver emits bare '\\n';
    a real pty maps that to CRLF (ONLCR), so we do the same before feeding."""
    screen = pyte.Screen(cols, rows)
    pyte.Stream(screen).feed(ansi.replace("\n", "\r\n"))
    return screen


def snap_stream(out: Path, *, limit: int | None, cols: int, rows: int | None) -> None:
    import capture_imp_ui as cap

    ansi = cap.stream_text(color=True, width=cols, limit=limit)
    n = len(ansi.rstrip("\n").split("\n"))
    # Auto-fit the whole slice (the point of a mockup); cap only as a sane
    # backstop against a pathologically long run. Use --limit to focus.
    height = rows or min(1000, max(10, n + 1))
    if n > height:
        print(f"note: {n} lines of output; image shows the last {height} "
              f"(use --limit to focus, or --rows to grow)")
    render_screen(_feed_screen(ansi, cols=cols, rows=height), str(out))


def snap_prompt(out: Path, *, type_text: str, cols: int, rows: int) -> None:
    from impty.session import TerminalSession

    argv = [sys.executable, str(_HERE / "capture_imp_ui.py"), "prompt"]
    sess = TerminalSession(argv, cols=cols, rows=rows)
    try:
        sess.wait_idle(idle=0.6, hard=15)
        if type_text:
            sess.send(type_text)
            sess.wait_idle(idle=0.5, hard=8)
        render_screen(sess._screen, str(out))
    finally:
        sess.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Render an imp UI scene to a PNG.")
    ap.add_argument("scene", choices=("stream", "prompt"))
    ap.add_argument("--limit", type=int, default=None, help="stream: first N events")
    ap.add_argument("--type", dest="type_text", default="", help="prompt: pre-typed text")
    ap.add_argument("--out", type=Path, default=None, help="PNG path")
    ap.add_argument("--cols", type=int, default=100)
    ap.add_argument("--rows", type=int, default=None, help="stream auto-fits; prompt default 16")
    ap.add_argument("--open", dest="open_it", action="store_true", help="xdg-open the PNG")
    a = ap.parse_args(argv)

    out = a.out or (_OUT_DIR / f"{a.scene}.png")
    out.parent.mkdir(parents=True, exist_ok=True)

    if a.scene == "stream":
        snap_stream(out, limit=a.limit, cols=a.cols, rows=a.rows)
    else:
        snap_prompt(out, type_text=a.type_text, cols=a.cols, rows=a.rows or 16)

    print(f"wrote {out}")
    if a.open_it:
        subprocess.run(["xdg-open", str(out)], check=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
