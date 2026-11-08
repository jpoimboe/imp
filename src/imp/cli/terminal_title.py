# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""TTY-only terminal-title writer: emits OSC window-title escapes; inert on non-TTY."""
from __future__ import annotations

from typing import Any

GLYPH_BUSY = "✦"  # next candidate: ⣿
GLYPH_IDLE = "ѱ"


class TerminalTitle:
    def __init__(
        self,
        stream: Any,
        quest_id: str,
        quest_title: str,
        initial: str = "busy",
        *,
        project: str = "",
    ) -> None:
        self._stream = stream
        self._quest_id = quest_id
        self._title = quest_title
        self._project = project
        self._enabled = bool(stream.isatty())
        self._current: str | None = None
        self._idle = initial == "idle"
        self._hold_glyph: str | None = None
        self._set(self._derive())

    def busy(self) -> None:
        self._idle = False
        self._set(self._derive())

    def idle(self) -> None:
        self._idle = True
        self._set(self._derive())

    def set_hold_glyph(self, glyph: str | None) -> None:
        self._hold_glyph = glyph
        self._set(self._derive())

    def reset(self) -> None:
        if not self._enabled:
            return
        self._stream.write("\x1b]2;\x07")
        self._stream.flush()
        self._current = None

    def update_title(self, title: str) -> None:
        if title == self._title:
            return
        self._title = title
        glyph = self._derive()
        # A title change does not change the derived glyph, so _set would short-circuit
        # on the unchanged glyph; clear _current to force a re-emit.
        self._current = None
        self._set(glyph)

    def _derive(self) -> str:
        if self._idle:
            return GLYPH_IDLE
        if self._hold_glyph is not None:
            return self._hold_glyph
        return GLYPH_BUSY

    def _set(self, glyph: str) -> None:
        if not self._enabled:
            return
        if glyph == self._current:
            return
        prefix = f"{glyph} " if glyph else ""
        label = " · ".join(
            p for p in (self._project, self._quest_id, self._title) if p)
        self._stream.write(f"\x1b]2;{prefix}{label}\x07")
        self._stream.flush()
        self._current = glyph
