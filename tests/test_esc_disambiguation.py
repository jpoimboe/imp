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
        os.close(leader); os.close(follower)


@pytest.mark.anyio
async def test_arrow_key_discarded() -> None:
    reader, leader, follower = _reader_on_pty()
    try:
        os.write(leader, b"\x1b[A")  # up-arrow: CSI + final byte 'A'
        assert await reader._disambiguate_esc() is False  # discarded, no fire
    finally:
        os.close(leader); os.close(follower)


@pytest.mark.anyio
async def test_alt_letter_fires_and_discards_byte() -> None:
    reader, leader, follower = _reader_on_pty()
    try:
        os.write(leader, b"\x1bf")  # ESC then a printable, non-introducer byte
        assert await reader._disambiguate_esc() is True
    finally:
        os.close(leader); os.close(follower)


@pytest.mark.anyio
async def test_esc_behind_arrow_still_fires() -> None:
    reader, leader, follower = _reader_on_pty()
    try:
        os.write(leader, b"\x1b[A\x1b")  # arrow, then a deliberate ESC
        assert await reader._disambiguate_esc() is True
    finally:
        os.close(leader); os.close(follower)


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
        os.close(leader); os.close(follower)


@pytest.mark.anyio
async def test_osc_consumed_to_terminator() -> None:
    reader, leader, follower = _reader_on_pty()
    try:
        os.write(leader, b"\x1b]11;rgb:ff/ff/ff\x07")  # OSC + BEL terminator
        assert await reader._disambiguate_esc() is False
    finally:
        os.close(leader); os.close(follower)


@pytest.mark.anyio
async def test_drain_stops_at_quiesce_boundary() -> None:
    reader, leader, follower = _reader_on_pty()
    try:
        os.write(leader, b"\x1b[")  # CSI introducer, no final byte yet
        reader._quiesce_depth = 1   # a prompt began quiescing mid-drain
        # Drain must stop and yield to prompt_toolkit (treated as consumed).
        assert await reader._disambiguate_esc() is False
    finally:
        os.close(leader); os.close(follower)
