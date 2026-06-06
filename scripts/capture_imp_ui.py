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
    python scripts/capture_imp_ui.py --list

Scenes that wait for input (prompt) block until driven; replay scenes print and
exit. `--color/--no-color` toggles ANSI (default: on).
"""

from __future__ import annotations

import argparse
import contextlib
import gzip
import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_RECORDED = _REPO / "tests" / "fixtures" / "cc_open_by_asking_full.jsonl.gz"


def _events() -> list[dict]:
    with gzip.open(_RECORDED, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def scene_stream(*, color: bool, width: int, limit: int | None) -> None:
    """Replay the recorded journal through the real LiveDriver to stdout.

    Append-only: the captured screen shows the tail (last `rows` lines). Pick
    `limit` to end the slice on the visual state you want to inspect — a small
    N for the header, a larger N to land on agent prose / a gate / completion."""
    from imp.cli.live_driver import LiveDriver
    from imp.cli.live_status import LiveStatus

    events = _events()
    if limit is not None:
        events = events[:limit]
    drv = LiveDriver(
        stream=sys.stdout, color=color, width=width, status=LiveStatus(sys.stdout)
    )
    for e in events:
        drv.on_event(e)
    drv.finish()
    sys.stdout.flush()


def scene_prompt(*, color: bool) -> None:
    """Show the real interactive input field (the current blue-bg `> ` prompt).

    Blocks on prompt_toolkit input so a driver can snapshot the empty field,
    type into it, and snapshot again. Ctrl-C/Ctrl-D end it."""
    from imp.cli.prompt import Prompt

    # Print a couple of lines of faux prior output so the prompt's separation
    # from preceding content is visible in the capture (matches real use, where
    # the field always follows agent output).
    sys.stdout.write("  …assistant output above…\n")
    sys.stdout.flush()
    p = Prompt(skills_dir=None, color=color)
    with contextlib.suppress(EOFError, KeyboardInterrupt):
        p.ask()


_SCENES = {"stream": "replay recorded journal", "prompt": "interactive input field"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Play one imp UI scene to the screen.")
    ap.add_argument("scene", nargs="?", choices=list(_SCENES))
    ap.add_argument("--list", action="store_true", help="list scenes and exit")
    ap.add_argument("--limit", type=int, default=None, help="stream: first N events")
    ap.add_argument("--width", type=int, default=100, help="render width (cols)")
    ap.add_argument("--no-color", dest="color", action="store_false")
    ap.set_defaults(color=True)
    a = ap.parse_args(argv)

    if a.list or not a.scene:
        for name, desc in _SCENES.items():
            print(f"  {name:8s} {desc}")
        return 0
    if a.scene == "stream":
        scene_stream(color=a.color, width=a.width, limit=a.limit)
    elif a.scene == "prompt":
        scene_prompt(color=a.color)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
