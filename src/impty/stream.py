# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""TerminalStream — a pyte ByteStream that drops the two escape-sequence classes
pyte mis-parses, before they reach the parser.

pyte discards the private prefix of a `<`/`=`/`>` CSI and dispatches the sequence
as if it were unprefixed, so a secondary DA (`ESC[>c`) answers as a primary DA and
the surplus reply is typed into the pane, and XTMODKEYS (`ESC[>4;2m`) lands as SGR
4;2 and underlines everything after it. pyte also does not implement string
sequences at all, drawing a DCS/SOS/PM/APC payload onto the grid as text. OSC and
`?`-prefixed CSI are deliberately left alone: pyte handles both correctly, and `?`
carries the DECSET alt-screen switch TerminalScreen's regime tracking depends on.
"""

from __future__ import annotations

import enum
from typing import Any

import pyte

_ESC = 0x1B
_CSI_INTRO = 0x5B  # '['
_PRIVATE_PREFIX = frozenset(b"<=>")


class _State(enum.Enum):
    GROUND = "ground"
    ESC = "esc"
    CSI = "csi"
    PRIVATE = "private"


class TerminalStream(pyte.ByteStream):
    """Byte-level filter in front of pyte's parser: the output is the input minus
    whole private-prefixed CSI sequences. The state alone encodes the held bytes,
    so at most two (`ESC`, `ESC [`) are ever pending across a chunk boundary.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._state = _State.GROUND

    # bytes narrows Stream.feed's str, exactly as pyte's own ByteStream.feed does.
    def feed(self, data: bytes) -> None:  # type: ignore[override]
        out = bytearray()
        i = 0
        while i < len(data):
            byte = data[i]
            i += 1
            if self._state is _State.GROUND:
                if byte == _ESC:
                    self._state = _State.ESC
                else:
                    out.append(byte)
            elif self._state is _State.ESC:
                if byte == _CSI_INTRO:
                    self._state = _State.CSI
                else:
                    # The byte ends this sequence without belonging to it:
                    # release what was held and re-read it in the new state.
                    out.append(_ESC)
                    self._state = _State.GROUND
                    i -= 1
            elif self._state is _State.CSI:
                if byte in _PRIVATE_PREFIX:
                    self._state = _State.PRIVATE
                else:
                    out += b"\x1b["
                    self._state = _State.GROUND
                    i -= 1
            elif self._state is _State.PRIVATE and 0x40 <= byte <= 0x7E:
                self._state = _State.GROUND  # final byte ends the sequence
        super().feed(bytes(out))
