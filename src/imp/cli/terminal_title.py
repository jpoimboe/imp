"""TTY-only terminal-title writer: emits OSC window-title escapes; inert on non-TTY."""
from __future__ import annotations

from typing import Any

GLYPH_WORKING = "●"
GLYPH_WAITING = "◌"


class TerminalTitle:
    def __init__(self, stream: Any, change_name: str) -> None:
        self._stream = stream
        self._name = change_name
        self._enabled = bool(stream.isatty())
        self.working()

    def working(self) -> None:
        self._set(GLYPH_WORKING)

    def waiting(self) -> None:
        self._set(GLYPH_WAITING)

    def reset(self) -> None:
        if not self._enabled:
            return
        self._stream.write("\x1b]2;\x07")
        self._stream.flush()

    def _set(self, glyph: str) -> None:
        if not self._enabled:
            return
        self._stream.write(f"\x1b]2;{glyph} imp — {self._name}\x07")
        self._stream.flush()
