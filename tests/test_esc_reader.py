"""EscKeyReader: opens /dev/tty cbreak, calls coord.signal_pause('esc') on
0x1b, releases termios for prompt_toolkit via tty_quiesced. In-process tests
redirect /dev/tty to a pty.openpty() slave via monkeypatched os.open."""

from __future__ import annotations

import contextlib
import os

import anyio
import pytest

from imp.cli.signals import EscKeyReader, InterruptCoordinator


def _can_pty() -> bool:
    try:
        import pty
        m, s = pty.openpty()
        os.close(m)
        os.close(s)
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _can_pty(), reason="pty not available")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _patch_dev_tty(monkeypatch: pytest.MonkeyPatch, slave_fd: int) -> None:
    real_open = os.open

    def fake_open(path: str, flags: int, mode: int = 0o777) -> int:
        if path == "/dev/tty":
            return os.dup(slave_fd)
        return real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", fake_open)


@pytest.mark.anyio
async def test_esc_byte_triggers_signal_pause_with_esc_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pty
    master, slave = pty.openpty()
    try:
        _patch_dev_tty(monkeypatch, slave)
        coord = InterruptCoordinator()
        reader = EscKeyReader(coord)
        async with anyio.create_task_group() as tg:
            tg.start_soon(reader.run)
            for _ in range(50):
                if coord._esc_reader is not None:
                    break
                await anyio.sleep(0.02)
            os.write(master, b"\x1b")
            for _ in range(50):
                if coord.should_pause:
                    break
                await anyio.sleep(0.02)
            tg.cancel_scope.cancel()
        assert coord.should_pause is True
        assert coord.pause_reason == "esc"
    finally:
        os.close(master)
        with contextlib.suppress(OSError):
            os.close(slave)


@pytest.mark.anyio
async def test_tty_quiesced_noop_before_run_starts() -> None:
    coord = InterruptCoordinator()
    reader = EscKeyReader(coord)
    coord.attach_esc_reader(reader)
    async with coord.tty_quiesced():
        pass


@pytest.mark.anyio
async def test_tty_quiesced_nested_idempotent_when_armed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pty
    import termios
    master, slave = pty.openpty()
    try:
        _patch_dev_tty(monkeypatch, slave)
        coord = InterruptCoordinator()
        reader = EscKeyReader(coord)
        async with anyio.create_task_group() as tg:
            tg.start_soon(reader.run)
            for _ in range(50):
                if coord._esc_reader is not None:
                    break
                await anyio.sleep(0.02)
            assert reader._fd is not None
            cbreak_attrs = termios.tcgetattr(reader._fd)
            async with coord.tty_quiesced():
                async with coord.tty_quiesced():
                    inner_attrs = termios.tcgetattr(reader._fd)
                    assert inner_attrs[3] == reader._orig_attrs[3]
                still_quiesced = termios.tcgetattr(reader._fd)
                assert still_quiesced[3] == reader._orig_attrs[3]
            for _ in range(20):
                final_attrs = termios.tcgetattr(reader._fd)
                if final_attrs[3] == cbreak_attrs[3]:
                    break
                await anyio.sleep(0.02)
            assert final_attrs[3] == cbreak_attrs[3]
            tg.cancel_scope.cancel()
    finally:
        os.close(master)
        with contextlib.suppress(OSError):
            os.close(slave)


@pytest.mark.anyio
async def test_run_returns_cleanly_when_dev_tty_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail_open(path: str, flags: int, mode: int = 0o777) -> int:
        if path == "/dev/tty":
            raise OSError("no tty")
        return os.open(path, flags, mode)

    monkeypatch.setattr(os, "open", _fail_open)
    coord = InterruptCoordinator()
    reader = EscKeyReader(coord)
    await reader.run()
    assert coord.should_pause is False
    assert coord.pause_reason is None
    assert coord._esc_reader is None


@pytest.mark.anyio
async def test_esc_during_quiesce_does_not_pause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC #3: while quiesced (a prompt owns the tty) an ESC byte must NOT
    trigger a pause. The synchronous park barrier makes this deterministic —
    tty_quiesced returns only once the reader has stopped reading /dev/tty."""
    import pty
    master, slave = pty.openpty()
    try:
        _patch_dev_tty(monkeypatch, slave)
        coord = InterruptCoordinator()
        reader = EscKeyReader(coord)
        async with anyio.create_task_group() as tg:
            tg.start_soon(reader.run)
            for _ in range(50):
                if coord._esc_reader is not None:
                    break
                await anyio.sleep(0.02)
            async with coord.tty_quiesced():       # barrier: reader is now parked
                os.write(master, b"\x1b")
                await anyio.sleep(0.15)             # reader must stay parked
                assert coord.should_pause is False
                tg.cancel_scope.cancel()           # tear down before unquiesce reads it
    finally:
        os.close(master)
        with contextlib.suppress(OSError):
            os.close(slave)
