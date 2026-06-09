# tests/test_tool_limits.py
from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from imp.agents.tools.limits import (
    FILE_READ_CAP_BYTES,
    ToolAborted,
    read_bytes_capped,
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


def test_read_text_capped_aborts_mid_read_between_chunks(tmp_path: Path) -> None:
    # Load-bearing: preemption must happen BETWEEN chunks, not only before the
    # first read. The coordinator stays clear for the first chunk's checkpoint,
    # then trips — so the abort happens partway through a multi-chunk read,
    # proving a runaway gigabyte read is abortable mid-stream.
    #
    # Threshold reasoning: tool_checkpoint reads `should_pause` once per loop
    # iteration (before each chunk read). With _checks > 1:
    #   check 1 (before chunk 1) → False → chunk 1 is read into buf
    #   check 2 (before chunk 2) → True  → ToolAborted raised
    # The abort therefore happens AFTER at least one chunk was consumed, not
    # before the first read — that is the mid-read property under test.
    from imp.agents.tools.limits import READ_CHUNK_BYTES

    class _FlipCoord:
        def __init__(self) -> None:
            self._checks = 0

        @property
        def should_pause(self) -> bool:
            self._checks += 1
            # Clear for the first checkpoint (before chunk 1), trip afterward.
            return self._checks > 1

    f = tmp_path / "multi.bin"
    f.write_bytes(b"q" * (READ_CHUNK_BYTES * 5))  # needs several chunks

    async def go() -> None:
        coord = _FlipCoord()
        with pytest.raises(ToolAborted) as ei:
            await read_text_capped(f, coordinator=coord)
        assert ei.value.reason == "interrupted"
        # Confirm at least two checkpoints fired: one clear pass (chunk 1 read)
        # and one trip (abort before chunk 2).
        assert coord._checks >= 2

    anyio.run(go)


def test_read_bytes_capped_returns_raw_bytes(tmp_path: Path) -> None:
    # Bytes-faithful: no decode, no U+FFFD — this is the primitive edit_file's
    # strict-decode path builds on.
    f = tmp_path / "raw.bin"
    f.write_bytes(b"abc\xff\x00def")

    async def go():
        return await read_bytes_capped(f)

    data, truncated = anyio.run(go)
    assert data == b"abc\xff\x00def"
    assert truncated is False


def test_read_bytes_capped_stops_at_cap(tmp_path: Path) -> None:
    f = tmp_path / "big.bin"
    f.write_bytes(b"x" * 500)

    async def go():
        return await read_bytes_capped(f, cap_bytes=100)

    data, truncated = anyio.run(go)
    assert data == b"x" * 100
    assert truncated is True


def test_read_bytes_capped_exactly_at_cap_reports_truncated(tmp_path: Path) -> None:
    # Documented conservative behavior: a file of EXACTLY cap_bytes returns
    # truncated=True though nothing was dropped. Callers st_size-disambiguate.
    f = tmp_path / "exact.bin"
    f.write_bytes(b"x" * 100)

    async def go():
        return await read_bytes_capped(f, cap_bytes=100)

    data, truncated = anyio.run(go)
    assert data == b"x" * 100
    assert truncated is True
