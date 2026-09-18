# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Render imp's terminal UI to the screen so it can be captured as an image.

A *scene player*: run as a subprocess on a real pty (e.g. under impty), it
reproduces one of imp's live visual states using the real render code — the
LiveDriver/LiveStatus for streaming output, the real prompt_toolkit Prompt for
the input field. A driver (scripts/snap_imp_ui.py or impty) spawns this, lets
it settle, and snapshots the rendered screen with impty.render.

No agent, no billing, no repo mutation: streaming scenes replay a *recorded*
event journal (`stream` replays tests/fixtures/cc_open_by_asking_full.jsonl.gz;
`stream-live` replays docs/examples/samo71.jsonl.gz). This exists to iterate on
imp's *look*, not its behavior.

Usage (run from repo root):
    python scripts/capture_imp_ui.py prompt              # interactive input field
    python scripts/capture_imp_ui.py stream [--limit N]  # replay first N events
    python scripts/capture_imp_ui.py stream-live --limit 930 --width 124
    python scripts/capture_imp_ui.py splash --all-scenes # render the splash pool
    python scripts/capture_imp_ui.py --list

Scenes that wait for input (prompt) block until driven; replay scenes print and
exit. `--color/--no-color` toggles ANSI (default: on). `stream-live` replays
docs/examples/samo71.jsonl.gz on a tty with the status bar live, always in color
(`--no-color` does not apply to it), and leaves the final footer on screen.
"""

from __future__ import annotations

import argparse
import contextlib
import gzip
import io
import json
import os
import random
import sys
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parent.parent
_RECORDED = _REPO / "tests" / "fixtures" / "cc_open_by_asking_full.jsonl.gz"

# Regenerate: imp events samo71 --all --json | jq -c '.[]' | gzip; re-redact event 256
DEMO_JOURNAL = _REPO / "docs" / "examples" / "samo71.jsonl.gz"

# Hero cut point: plan 1/3 · task 1/3, with the coder subagent mid-flight.
HERO_LIMIT = 930
HERO_WIDTH = 124

# Every clock the live footer reads is pointed at this instant (samo71's merge).
_PINNED_INSTANT = datetime(2026, 9, 18, 5, 38, 3, tzinfo=UTC)
_PINNED_FRAME = 0
_PINNED_SEED = 0


def _events() -> list[dict]:
    with gzip.open(_RECORDED, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def journal_events(path: Path, limit: int | None = None) -> list[dict]:
    """A journal's events, one dict per line; `limit` truncates to the first N."""
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        events = [json.loads(line) for line in fh if line.strip()]
    return events if limit is None else events[:limit]


def shift_to_instant(events: list[dict], instant: datetime) -> list[dict]:
    """Every time-bearing field moved by ONE delta, so the last event lands
    exactly on `instant`: a driver measuring these stamps against a pinned clock
    then renders each block's real age instead of its age against today."""
    delta = instant - datetime.fromisoformat(events[-1]["timestamp"])
    seconds = round(delta.total_seconds())
    shifted: list[dict] = []
    for event in events:
        moved = dict(event)
        moved["timestamp"] = (
            datetime.fromisoformat(event["timestamp"]) + delta).isoformat()
        payload = moved.get("payload")
        if isinstance(payload, dict) and isinstance(payload.get("windows"), list):
            payload = dict(payload)
            payload["windows"] = [
                {**w, "resets_at": w["resets_at"] + seconds}
                if isinstance(w, dict) and isinstance(w.get("resets_at"), int)
                else w
                for w in payload["windows"]]
            moved["payload"] = payload
        shifted.append(moved)
    return shifted


@contextlib.contextmanager
def _pinned_clocks(instant: datetime, width: int) -> Iterator[None]:
    """Point the driver's two independent clocks at `instant`, fix the terminal
    width, and seed the RNG: compose_status reads datetime.now(UTC), the quota
    countdown falls through to time.time(), term_width() would read the real
    terminal, and the rotating status phrase draws from random."""
    from imp.cli import live_driver as ld

    class _Clock:
        @staticmethod
        def now(tz: Any = None) -> datetime:
            return instant

    class _Epoch:
        @staticmethod
        def time() -> float:
            return instant.timestamp()

    saved_clock, saved_epoch = ld.datetime, ld.time
    saved_columns = os.environ.get("COLUMNS")
    saved_rng = random.getstate()
    ld.datetime, ld.time = _Clock, _Epoch
    os.environ["COLUMNS"] = str(width)
    random.seed(_PINNED_SEED)      # narrative's rotating status phrase draws from random
    try:
        yield
    finally:
        ld.datetime, ld.time = saved_clock, saved_epoch
        if saved_columns is None:
            os.environ.pop("COLUMNS", None)
        else:
            os.environ["COLUMNS"] = saved_columns
        random.setstate(saved_rng)


def _pinned_status(stream: Any) -> Any:
    """A LiveStatus whose composed frame index never moves: the scene runs no
    tick task, so the spinner must not depend on which frame a thread reached."""
    from imp.cli.live_status import LiveStatus

    class _PinnedStatus(LiveStatus):
        def update(self, compose: Callable[[int], list[str] | None]) -> None:
            super().update(lambda _frame: compose(_PINNED_FRAME))

    return _PinnedStatus(stream)


def replay_live(stream: Any, *, journal: Path = DEMO_JOURNAL,
                limit: int = HERO_LIMIT, width: int = HERO_WIDTH) -> None:
    """Replay the first `limit` events to `stream` with LiveStatus active, so a
    tty keeps the transient footer on screen. Deterministic: see _pinned_clocks,
    _pinned_status, and the seeded status-phrase RNG."""
    from imp.cli import palette as pal
    from imp.cli.live_driver import LiveDriver

    pal.set_theme("dark")
    events = shift_to_instant(journal_events(journal, limit), _PINNED_INSTANT)
    with _pinned_clocks(_PINNED_INSTANT, width):
        driver = LiveDriver(stream=stream, color=True, width=width,
                            status=_pinned_status(stream))
        for event in events:
            driver.on_event(event)


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


def scene_stream_live(*, journal: Path, limit: int, width: int) -> None:
    """Replay a journal to REAL stdout, so a pty keeps the live status bar on
    screen — the frame the non-tty `stream` scene cannot produce."""
    replay_live(sys.stdout, journal=journal, limit=limit, width=width)
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


_SCENES = {"stream": "replay recorded journal",
           "stream-live": "replay the demo journal on a pty, status bar live",
           "prompt": "interactive input field",
           "splash": "startup splash scene"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Play one imp UI scene to the screen.")
    ap.add_argument("scene", nargs="?", choices=list(_SCENES))
    ap.add_argument("--list", action="store_true", help="list scenes and exit")
    ap.add_argument("--limit", type=int, default=None, help="stream: first N events")
    ap.add_argument("--journal", type=Path, default=DEMO_JOURNAL,
                    help="stream-live: journal to replay")
    ap.add_argument("--width", type=int, default=None, help="render width (cols)")
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
            print(f"  {name:11s} {desc}")
        return 0
    if a.all_scenes and a.scene != "splash":
        ap.error("--all-scenes requires the splash scene")
    if a.scene == "stream":
        scene_stream(color=a.color, width=a.width or 100, limit=a.limit)
    elif a.scene == "stream-live":
        scene_stream_live(journal=a.journal,
                          limit=a.limit if a.limit is not None else HERO_LIMIT,
                          width=a.width or HERO_WIDTH)
    elif a.scene == "prompt":
        scene_prompt(color=a.color)
    elif a.scene == "splash":
        scene_splash(color=a.color, width=a.width or 100, theme=a.theme,
                     index=a.scene_index, all_scenes=a.all_scenes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
