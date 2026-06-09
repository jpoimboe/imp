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


def test_watchdog_exceeds_tool_wallclock_with_margin() -> None:
    """F4 coupling (spec: heartbeat prerequisite): a tool's watchdog window
    opens at the PREVIOUS heartbeat — with the post-API heartbeat in place the
    margin is scheduling headroom; without it the window would absorb full
    model-API latency. Either way, at zero margin a foreground bash legally
    approaching its B.1 wall clock dies as a `stalled` config error instead of
    returning the ToolAborted("timed-out") is_error the agent can react to."""
    from imp.agents.tools.limits import (
        AGENT_INACTIVITY_TIMEOUT_S,
        TOOL_WALLCLOCK_TIMEOUT_S,
    )
    assert AGENT_INACTIVITY_TIMEOUT_S > TOOL_WALLCLOCK_TIMEOUT_S
    assert AGENT_INACTIVITY_TIMEOUT_S - TOOL_WALLCLOCK_TIMEOUT_S >= 300


def test_bg_wait_budget_well_under_watchdog() -> None:
    """F4 coupling: each completed bash_output(wait=...) heartbeats and resets
    the watchdog window, but the budget must leave margin for the same turn's
    remaining latency. A future watchdog reduction must not silently invert
    this relationship."""
    from imp.agents.tools.limits import AGENT_INACTIVITY_TIMEOUT_S, BG_WAIT_MAX_S
    assert BG_WAIT_MAX_S <= AGENT_INACTIVITY_TIMEOUT_S - 300


def test_bg_stream_cap_not_aliased_to_read_cap() -> None:
    """BG_STREAM_FILE_CAP_BYTES is a KILL-POLICY trigger; FILE_READ_CAP_BYTES is
    a READ bound. They happen to share a value today (both 5_000_000) — assert
    the bg cap is defined as its OWN literal, not an alias, so a future
    read-cap bump forces a conscious decision about kill policy."""
    import inspect
    import re
    from imp.agents.tools import limits
    assert limits.BG_STREAM_FILE_CAP_BYTES == 5_000_000
    src = inspect.getsource(limits)
    m = re.search(r"^BG_STREAM_FILE_CAP_BYTES = ([^#\n]+)", src, re.M)
    assert m is not None
    assert m.group(1).strip() == "5_000_000"     # a literal, not an alias
