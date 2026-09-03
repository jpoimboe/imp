# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Render imp's terminal UI to the screen so it can be captured as an image.

A *scene player*: run as a subprocess on a real pty (e.g. under impty), it
reproduces one of imp's live visual states using the real render code — the
LiveDriver/LiveStatus for streaming output, the real prompt_toolkit Prompt for
the input field. A driver (scripts/snap_imp_ui.py or impty) spawns this, lets
it settle, and snapshots the rendered screen with impty.render.

No agent, no billing, no repo mutation: streaming scenes replay a *recorded*
event journal (tests/fixtures/cc_open_by_asking_full.jsonl.gz). This exists to
iterate on imp's *look*, not its behavior.

Usage (run from repo root):
    python scripts/capture_imp_ui.py prompt              # interactive input field
    python scripts/capture_imp_ui.py stream [--limit N]  # replay first N events
    python scripts/capture_imp_ui.py splash --all-scenes # render the splash pool
    python scripts/capture_imp_ui.py --list

Scenes that wait for input (prompt) block until driven; replay scenes print and
exit. `--color/--no-color` toggles ANSI (default: on).
"""

from __future__ import annotations

import argparse
import contextlib
import gzip
import io
import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_RECORDED = _REPO / "tests" / "fixtures" / "cc_open_by_asking_full.jsonl.gz"


def _events() -> list[dict]:
    with gzip.open(_RECORDED, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


class _Pipe(io.StringIO):
    """A non-tty sink so LiveStatus stays a no-op during offline replay."""

    def isatty(self) -> bool:
        return False


def stream_text(*, color: bool, width: int, limit: int | None,
                theme: str = "dark") -> str:
    """The live body the recorded journal produces, as a string — replayed
    through the real LiveDriver. `limit` truncates to the first N events so a
    slice ends on the visual state you want (header, prose, gate, completion).
    Reused by scene_stream (prints it) and snap_imp_ui (renders it to a PNG)."""
    from imp.cli import palette as pal
    from imp.cli.live_driver import LiveDriver
    from imp.cli.live_status import LiveStatus

    # The mockup renders the truecolor hexes imp emits for `theme`, so the PNG
    # matches a real terminal on that theme. Plain (color=False) is unaffected.
    pal.set_theme(theme)
    events = _events()
    if limit is not None:
        events = events[:limit]
    buf = _Pipe()
    drv = LiveDriver(stream=buf, color=color, width=width, status=LiveStatus(buf))
    for e in events:
        drv.on_event(e)
    drv.finish()
    return buf.getvalue()


def scene_stream(*, color: bool, width: int, limit: int | None) -> None:
    """Replay the recorded journal through the real LiveDriver to stdout."""
    sys.stdout.write(stream_text(color=color, width=width, limit=limit))
    sys.stdout.flush()


def scene_prompt(*, color: bool) -> None:
    """Show the real interactive input field (the current blue-bg `> ` prompt).

    Blocks on prompt_toolkit input so a driver can snapshot the empty field,
    type into it, and snapshot again. Ctrl-C/Ctrl-D end it."""
    from imp.cli import palette as pal
    from imp.cli.prompt import Prompt

    pal.set_theme("dark")   # mockup showcases the dark truecolor theme

    # Print a couple of lines of faux prior output so the prompt's separation
    # from preceding content is visible in the capture (matches real use, where
    # the field always follows agent output).
    sys.stdout.write("  …assistant output above…\n")
    sys.stdout.flush()
    p = Prompt(skills_dir=None, color=color)
    with contextlib.suppress(EOFError, KeyboardInterrupt):
        p.ask()


def scene_splash(*, color: bool, width: int, theme: str = "dark",
                 index: int = 0, all_scenes: bool = False) -> None:
    from imp.cli import palette as pal
    from imp.cli import splash

    pal.set_theme(theme)
    indices = range(len(splash.SCENES)) if all_scenes else (index,)
    for position, scene_index in enumerate(indices):
        builder, phrase = splash.SCENES[scene_index]
        lines = splash.scene_lines(scene_index, theme=theme, color=color)
        if all_scenes:
            if position:
                sys.stdout.write("\n")
            sys.stdout.write(f"[{scene_index:02d}] {builder.__name__}\n")
        sys.stdout.write("\n".join(lines) + "\n" + phrase + "\n")
    sys.stdout.flush()


_SCENES = {"stream": "replay recorded journal", "prompt": "interactive input field",
           "splash": "startup splash scene"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Play one imp UI scene to the screen.")
    ap.add_argument("scene", nargs="?", choices=list(_SCENES))
    ap.add_argument("--list", action="store_true", help="list scenes and exit")
    ap.add_argument("--limit", type=int, default=None, help="stream: first N events")
    ap.add_argument("--width", type=int, default=100, help="render width (cols)")
    ap.add_argument("--no-color", dest="color", action="store_false")
    ap.add_argument("--theme", default="dark", help="splash: theme (dark/light/ansi)")
    splash_choice = ap.add_mutually_exclusive_group()
    splash_choice.add_argument("--scene-index", type=int, default=0,
                               help="splash: index into splash.SCENES")
    splash_choice.add_argument("--all-scenes", action="store_true",
                               help="splash: render every scene in gallery order")
    ap.set_defaults(color=True)
    a = ap.parse_args(argv)

    if a.list or not a.scene:
        for name, desc in _SCENES.items():
            print(f"  {name:8s} {desc}")
        return 0
    if a.all_scenes and a.scene != "splash":
        ap.error("--all-scenes requires the splash scene")
    if a.scene == "stream":
        scene_stream(color=a.color, width=a.width, limit=a.limit)
    elif a.scene == "prompt":
        scene_prompt(color=a.color)
    elif a.scene == "splash":
        scene_splash(color=a.color, width=a.width, theme=a.theme,
                     index=a.scene_index, all_scenes=a.all_scenes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
