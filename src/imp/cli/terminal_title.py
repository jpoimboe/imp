"""TTY-only terminal-title writer: emits OSC window-title escapes; inert on non-TTY."""
from __future__ import annotations

from typing import Any

GLYPH_WORKING = "•"
GLYPH_WAITING = "Ⲫ"


class TerminalTitle:
    def __init__(self, stream: Any, change_id: str, change_name: str) -> None:
        self._stream = stream
        self._change_id = change_id
        self._name = change_name
        self._enabled = bool(stream.isatty())
        self._current: str | None = None
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
        self._current = None

    def update_name(self, name: str) -> None:
        if name == self._name:
            return
        self._name = name
        glyph = self._current if self._current is not None else GLYPH_WORKING
        # A rename does not change the working/waiting state, so _set would
        # short-circuit on the unchanged glyph; clear _current to force a re-emit.
        self._current = None
        self._set(glyph)

    def _set(self, glyph: str) -> None:
        if not self._enabled:
            return
        if glyph == self._current:
            return
        prefix = f"{glyph} " if glyph else ""
        label = (
            f"imp · {self._change_id} · {self._name}"
            if self._name
            else f"imp · {self._change_id}"
        )
        self._stream.write(f"\x1b]2;{prefix}{label}\x07")
        self._stream.flush()
        self._current = glyph
