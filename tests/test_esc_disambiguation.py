from __future__ import annotations

import os
import pty

import pytest

from imp.cli.signals import EscKeyReader, InterruptCoordinator


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _reader_on_pty() -> tuple[EscKeyReader, int, int]:
    """A reader wired to a pty slave fd, with an injected fast sequence window."""
    import tty
    leader, follower = pty.openpty()
    tty.setcbreak(follower)  # mirror what EscKeyReader.run() does; pty canonical mode drops \x1b
    coord = InterruptCoordinator()
    reader = EscKeyReader(coord)
    reader._fd = follower
    reader._seq_timeout_s = 0.02  # injected window
    return reader, leader, follower


@pytest.mark.anyio
async def test_lone_esc_fires() -> None:
    reader, leader, follower = _reader_on_pty()
    try:
        os.write(leader, b"\x1b")
        # No follow-up byte → window expires → real ESC.
        assert await reader._disambiguate_esc() is True
    finally:
        os.close(leader)
        os.close(follower)


@pytest.mark.anyio
async def test_arrow_key_discarded() -> None:
    reader, leader, follower = _reader_on_pty()
    try:
        os.write(leader, b"\x1b[A")  # up-arrow: CSI + final byte 'A'
        assert await reader._disambiguate_esc() is False  # discarded, no fire
    finally:
        os.close(leader)
        os.close(follower)


@pytest.mark.anyio
async def test_alt_letter_fires_and_discards_byte() -> None:
    reader, leader, follower = _reader_on_pty()
    try:
        os.write(leader, b"\x1bf")  # ESC then a printable, non-introducer byte
        assert await reader._disambiguate_esc() is True
    finally:
        os.close(leader)
        os.close(follower)


@pytest.mark.anyio
async def test_esc_behind_arrow_still_fires() -> None:
    reader, leader, follower = _reader_on_pty()
    try:
        os.write(leader, b"\x1b[A\x1b")  # arrow, then a deliberate ESC
        assert await reader._disambiguate_esc() is True
    finally:
        os.close(leader)
        os.close(follower)


@pytest.mark.anyio
async def test_unterminated_osc_abandons_then_later_esc_fires() -> None:
    reader, leader, follower = _reader_on_pty()
    try:
        os.write(leader, b"\x1b]11;rgb:00/00/00")  # OSC, no terminator
        # Drain abandons on the per-byte timeout; the sequence is treated as
        # consumed (False), and nothing reaches an interject sink.
        assert await reader._disambiguate_esc() is False
        # A later real ESC still fires.
        os.write(leader, b"\x1b")
        assert await reader._disambiguate_esc() is True
    finally:
        os.close(leader)
        os.close(follower)


@pytest.mark.anyio
async def test_osc_consumed_to_terminator() -> None:
    reader, leader, follower = _reader_on_pty()
    try:
        os.write(leader, b"\x1b]11;rgb:ff/ff/ff\x07")  # OSC + BEL terminator
        assert await reader._disambiguate_esc() is False
    finally:
        os.close(leader)
        os.close(follower)


@pytest.mark.anyio
async def test_drain_stops_at_quiesce_boundary() -> None:
    reader, leader, follower = _reader_on_pty()
    try:
        os.write(leader, b"\x1b[")  # CSI introducer, no final byte yet
        reader._quiesce_depth = 1   # a prompt began quiescing mid-drain
        # Drain must stop and yield to prompt_toolkit (treated as consumed).
        assert await reader._disambiguate_esc() is False
    finally:
        os.close(leader)
        os.close(follower)


@pytest.mark.anyio
async def test_drain_cap_abandoned_without_wedging() -> None:
    """_drain_until exits via _DRAIN_CAP when buffered non-final bytes exceed the cap.

    We test _drain_until directly (not through _disambiguate_esc) so that
    leftover bytes are not re-consumed by the outer loop.  With cap=8 and 20
    non-final bytes written (all immediately buffered), a regressed ``while True``
    would read all 20 bytes and then block for the full seq_timeout_s=1.0 on
    each subsequent call — making the test take 8+ seconds.  With the cap, all 8
    iterations read buffered bytes (fast), then the loop exits, leaving 12 bytes.

    Discriminating: with the cap → 12 bytes remain; without the cap → drain
    reads all 20 and then times out once per cap iteration (would be very slow).
    The assertion on leftover count catches cap removal."""
    reader, leader, follower = _reader_on_pty()
    try:
        # 20 non-final bytes (0x20 space, outside 0x40-0x7e final range),
        # all written at once so they're immediately buffered in the pty.
        os.write(leader, b" " * 20)
        # Small cap: drain stops after 8 reads, leaving 12 bytes.
        reader._DRAIN_CAP = 8         # type: ignore[attr-defined]
        # Long per-byte window so only the cap (not per-byte timeout) terminates.
        reader._seq_timeout_s = 1.0
        # With cap=8: reads 8 spaces (fast, all buffered), exits → returns quickly.
        # Without cap (while True): reads all 20, then waits 1.0 s per additional
        # _poll_byte call — test would be extremely slow / appear hung.
        await reader._drain_until(lambda b: 0x40 <= b[0] <= 0x7e)
        # Discriminating assertion: 12 bytes (20 written - 8 cap) must remain.
        # A cap-less drain would have consumed all 20 and left nothing readable.
        import select as _select
        ready, _, _ = _select.select([follower], [], [], 0.1)
        assert ready, "pty buffer empty — cap did NOT fire (drain consumed all 20 bytes)"
        leftover = os.read(follower, 64)
        assert len(leftover) == 12, (
            f"expected 12 leftover bytes (20 written - 8 cap), got {len(leftover)}: {leftover!r}"
        )
    finally:
        os.close(leader)
        os.close(follower)


@pytest.mark.anyio
async def test_drain_yields_buffered_bytes_to_prompt_on_quiesce() -> None:
    """The top-of-loop quiesce guard in _drain_until returns BEFORE reading any bytes.

    We test _drain_until directly to avoid the outer _disambiguate_esc loop
    re-consuming leftover bytes.  Post-introducer bytes are ALL non-final spaces
    (0x20, outside the 0x40-0x7e final-byte range).

    Discriminating:
    - WITH quiesce guard: _drain_until returns at the top of the first iteration
      (before calling _poll_byte) → all 5 bytes remain.
    - WITHOUT quiesce guard: _drain_until calls _poll_byte for each space,
      exhausts the buffer, then times out → 0 bytes remain (leftover = b"").

    The assertion that bytes remain after the call is ONLY satisfied when the
    guard fires before any reads occur."""
    reader, leader, follower = _reader_on_pty()
    try:
        # 5 non-final bytes: quiesce is active before drain is entered.
        os.write(leader, b" " * 5)
        reader._quiesce_depth = 1
        # With guard: returns at top of loop (before any _poll_byte) → fast.
        # Without guard: reads all 5 spaces then times out → bytes gone.
        await reader._drain_until(lambda b: 0x40 <= b[0] <= 0x7e)
        # All 5 bytes must still be readable — guard returned before any reads.
        import select as _select
        ready, _, _ = _select.select([follower], [], [], 0.2)
        assert ready, (
            "post-introducer bytes were consumed by the drain — "
            "quiesce guard did not fire before reading"
        )
        leftover = os.read(follower, 64)
        assert len(leftover) == 5, (
            f"expected 5 bytes to remain (guard fired before reads), "
            f"got {len(leftover)}: {leftover!r}"
        )
    finally:
        os.close(leader)
        os.close(follower)
