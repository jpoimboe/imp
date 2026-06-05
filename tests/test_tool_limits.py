# tests/test_tool_limits.py
from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from imp.agents.tools.limits import (
    FILE_READ_CAP_BYTES,
    ToolAborted,
    read_text_capped,
    tool_checkpoint,
)


class _Coord:
    def __init__(self, should_pause: bool = False) -> None:
        self.should_pause = should_pause


def test_checkpoint_passes_when_clear() -> None:
    # No coordinator, no deadline -> just yields, never raises.
    anyio.run(tool_checkpoint, None, None)


def test_checkpoint_raises_interrupted_on_pause() -> None:
    async def go() -> None:
        with pytest.raises(ToolAborted) as ei:
            await tool_checkpoint(_Coord(should_pause=True), None)
        assert ei.value.reason == "interrupted"
    anyio.run(go)


def test_checkpoint_raises_timed_out_on_past_deadline() -> None:
    async def go() -> None:
        past = anyio.current_time() - 1.0
        with pytest.raises(ToolAborted) as ei:
            await tool_checkpoint(None, past)
        assert ei.value.reason == "timed-out"
    anyio.run(go)


def test_read_text_capped_small_file_not_truncated(tmp_path: Path) -> None:
    f = tmp_path / "small.txt"
    f.write_text("hello\nworld\n")
    text, truncated = anyio.run(lambda: _read(f))
    assert text == "hello\nworld\n"
    assert truncated is False


async def _read(path: Path):
    return await read_text_capped(path)


def test_read_text_capped_single_giant_no_newline_line(tmp_path: Path) -> None:
    # The incident shape: one enormous line with NO newline. A naive line reader
    # would buffer the whole thing; the bounded reader must stop at the byte cap.
    f = tmp_path / "giant.txt"
    f.write_bytes(b"x" * 500_000)  # 500 KB, zero newlines

    async def go():
        return await read_text_capped(f, cap_bytes=1000)

    text, truncated = anyio.run(go)
    assert truncated is True
    assert len(text) == 1000          # stopped AT the byte ceiling, mid-line
    assert "\n" not in text


def test_read_text_capped_aborts_on_pause_promptly(tmp_path: Path) -> None:
    f = tmp_path / "big.txt"
    f.write_bytes(b"y" * (FILE_READ_CAP_BYTES))  # large enough to need many chunks

    async def go():
        coord = _Coord(should_pause=True)
        with pytest.raises(ToolAborted) as ei:
            await read_text_capped(f, coordinator=coord)
        assert ei.value.reason == "interrupted"

    anyio.run(go)
