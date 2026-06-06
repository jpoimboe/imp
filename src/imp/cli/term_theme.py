"""Detect the terminal's color theme ONCE, at `imp init`, so the choice is
persisted to config instead of re-queried (and risking a hang) on every run.

`detect_theme()` returns one of:
  ansi  — the terminal isn't truecolor, OR the background couldn't be told. imp
          emits the 16 named ANSI colors and the terminal paints them (adaptive).
  dark  — truecolor terminal with a dark background.
  light — truecolor terminal with a light background.

Background detection is best-effort and never blocks longer than `timeout`:
COLORFGBG (instant, if exported) → an OSC 11 query (ask the terminal its
background; it replies on stdin) → default dark. Any failure falls back, so a
non-responsive terminal degrades to a sensible default rather than stalling.
"""
from __future__ import annotations

import contextlib
import os
import re
import select
import sys
import time


def detect_theme(*, timeout: float = 0.1) -> str:
    """Resolve the persisted theme: ansi | dark | light. See module docstring."""
    if not _supports_truecolor():
        return "ansi"
    return _detect_background(timeout=timeout) or "dark"


def _supports_truecolor() -> bool:
    return os.environ.get("COLORTERM", "").strip().lower() in ("truecolor", "24bit")


def _detect_background(*, timeout: float) -> str | None:
    """'dark' | 'light' | None — None when neither signal is conclusive."""
    from_env = _background_from_colorfgbg(os.environ.get("COLORFGBG"))
    if from_env is not None:
        return from_env
    rgb = _query_osc11_background(timeout=timeout)
    if rgb is not None:
        return "light" if _luminance(rgb) >= 0.5 else "dark"
    return None


def _background_from_colorfgbg(value: str | None) -> str | None:
    """Parse COLORFGBG ("fg;bg" or "fg;default;bg"). By the rxvt/Konsole
    convention the bg index 0-6 and 8 are dark, 7 and 9-15 are light."""
    if not value:
        return None
    parts = value.split(";")
    if len(parts) < 2:
        return None
    try:
        bg = int(parts[-1])
    except ValueError:
        return None
    return "dark" if bg in (0, 1, 2, 3, 4, 5, 6, 8) else "light"


_OSC11_RE = re.compile(r"rgba?:([0-9a-fA-F]+)/([0-9a-fA-F]+)/([0-9a-fA-F]+)")


def _query_osc11_background(*, timeout: float) -> tuple[int, int, int] | None:
    """Ask the terminal its background via OSC 11 and parse the reply, returning
    (r, g, b) in 0-255 — or None if there's no usable TTY, no termios (non-POSIX),
    or no answer within `timeout`. Always restores terminal attributes."""
    try:
        import termios
        import tty
    except ImportError:
        return None  # non-POSIX (e.g. Windows): no raw-mode query
    stdin, stdout = sys.__stdin__, sys.__stdout__
    if not (stdin and stdout and stdin.isatty() and stdout.isatty()):
        return None
    fd = stdin.fileno()
    try:
        old = termios.tcgetattr(fd)
    except (termios.error, ValueError, OSError):
        return None
    try:
        tty.setraw(fd)
        stdout.write("\x1b]11;?\x07")
        stdout.flush()
        # OSC 11 reply ends with BEL (\x07) or ST (\x1b\\).
        buf = _read_until(fd, ("\x07", "\x1b\\"), timeout=timeout)
    except (OSError, ValueError):
        return None
    finally:
        with contextlib.suppress(termios.error, ValueError, OSError):
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    m = _OSC11_RE.search(buf)
    return _parse_components(m.groups()) if m else None


def _read_until(fd: int, terminators: tuple[str, ...], *, timeout: float) -> str:
    """Read from `fd` until a terminator appears or `timeout` elapses."""
    end = time.monotonic() + timeout
    buf = ""
    while True:
        remaining = end - time.monotonic()
        if remaining <= 0:
            break
        ready, _, _ = select.select([fd], [], [], remaining)
        if not ready:
            break
        try:
            chunk = os.read(fd, 64)
        except OSError:
            break
        if not chunk:
            break
        buf += chunk.decode("latin-1", "replace")
        if any(t in buf for t in terminators):
            break
    return buf


def _parse_components(groups: tuple[str, ...]) -> tuple[int, int, int] | None:
    """Scale three variable-width hex components (e.g. '1e1e') to 0-255 each."""
    try:
        vals = []
        for comp in groups:
            scale = 16 ** len(comp) - 1
            if scale <= 0:
                return None
            vals.append(round(int(comp, 16) / scale * 255))
        return (vals[0], vals[1], vals[2])
    except ValueError:
        return None


def _luminance(rgb: tuple[int, int, int]) -> float:
    """Rec. 709 relative luminance, 0.0 (black) .. 1.0 (white)."""
    r, g, b = rgb
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0
