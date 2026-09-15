"""The impty snapshot path must hand claude_code a payload its transport can carry.

Auto-fit rasterizes under IMAGE_MAX_DIM, so process_image passes the PNG through
untouched — no resize, no re-encode. That resample was the inflation: it turned crisp
terminal text into antialiased gradients PNG compresses far worse, pushing a routine
screenshot past the CLI's per-line buffer (sized in tests/test_claude_code_line_buffer.py).
This test ties the two halves together; it lives on the imp side so tests/impty/ stays
free of imp imports.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pyte

from imp.agents.tools.image import process_image
from imp.providers.claude import _CLI_LINE_BUFFER_BYTES
from impty.render import render_screen
from impty.screen import TerminalScreen


def _dense_screen(cols: int, rows: int) -> TerminalScreen:
    """Every cell painted with a printable glyph — the worst case for PNG size, and
    what a real TUI capture looks like."""
    screen = TerminalScreen(cols, rows, history=100, ratio=0.5)
    stream = pyte.ByteStream(screen)
    body = "".join(chr(33 + (i % 90)) for i in range(cols)).encode()
    for row in range(rows):
        stream.feed(body + (b"\r\n" if row < rows - 1 else b""))
    return screen


def test_a_dense_snapshot_passes_through_unresampled_and_fits_the_line_buffer(
    tmp_path: Path,
) -> None:
    png = Path(render_screen(_dense_screen(150, 44), str(tmp_path / "snap.png")))
    raw = png.read_bytes()
    processed = process_image(raw)
    assert processed.data == raw, (
        f"a fitted snapshot must take process_image's passthrough branch, but "
        f"{len(raw)} bytes came back as {len(processed.data)} ({processed.width}x"
        f"{processed.height}): it was resized or re-encoded, which is the inflation "
        f"that overran the transport's line budget."
    )
    line_bytes = 2 * len(base64.b64encode(processed.data))
    assert line_bytes < _CLI_LINE_BUFFER_BYTES, (
        f"the CLI echoes an image tool result twice on one stream-json line, so this "
        f"snapshot lands as a {line_bytes}-byte line against a "
        f"{_CLI_LINE_BUFFER_BYTES}-byte buffer."
    )
