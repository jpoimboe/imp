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
async def test_interject_sink_captures_typed_line_during_thinking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Task 11: with an interject sink attached, a line typed during agent-thinking
    # (no prompt up) is assembled from raw bytes and delivered on Enter — the live
    # source of interject lines, coexisting with ESC on the one /dev/tty.
    import pty
    master, slave = pty.openpty()
    try:
        _patch_dev_tty(monkeypatch, slave)
        coord = InterruptCoordinator()
        captured: list[str] = []
        reader = EscKeyReader(coord, on_interject=captured.append)
        async with anyio.create_task_group() as tg:
            tg.start_soon(reader.run)
            for _ in range(50):
                if coord._esc_reader is not None:
                    break
                await anyio.sleep(0.02)
            os.write(master, b"fix the typo\r")
            for _ in range(50):
                if captured:
                    break
                await anyio.sleep(0.02)
            tg.cancel_scope.cancel()
        assert captured == ["fix the typo"]
        assert coord.should_pause is False     # a typed line is not a pause
    finally:
        os.close(master)
        with contextlib.suppress(OSError):
            os.close(slave)


@pytest.mark.anyio
async def test_esc_discards_partial_interject_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ESC mid-line pauses AND discards the partial buffer (never delivered).
    import pty
    master, slave = pty.openpty()
    try:
        _patch_dev_tty(monkeypatch, slave)
        coord = InterruptCoordinator()
        captured: list[str] = []
        reader = EscKeyReader(coord, on_interject=captured.append)
        async with anyio.create_task_group() as tg:
            tg.start_soon(reader.run)
            for _ in range(50):
                if coord._esc_reader is not None:
                    break
                await anyio.sleep(0.02)
            os.write(master, b"partial")     # no Enter
            await anyio.sleep(0.1)
            os.write(master, b"\x1b")         # ESC before submitting
            for _ in range(50):
                if coord.should_pause:
                    break
                await anyio.sleep(0.02)
            tg.cancel_scope.cancel()
        assert coord.should_pause is True
        assert coord.pause_reason == "esc"
        assert captured == []                 # partial line never delivered
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
async def test_run_arms_before_task_start_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`await tg.start(reader.run)` must not return until the reader has armed
    (attached + cbreak). Otherwise a workflow whose FIRST step is an interactive
    prompt quiesces against an unattached reader (a no-op), prompt_toolkit
    configures the terminal, and the reader then arms mid-prompt — its
    `tty.setcbreak` clobbers prompt_toolkit's input mode and the prompt can never
    read a key. (The human-gate e2e hang.)"""
    import pty

    master, slave = pty.openpty()
    try:
        _patch_dev_tty(monkeypatch, slave)
        coord = InterruptCoordinator()
        reader = EscKeyReader(coord)
        async with anyio.create_task_group() as tg:
            await tg.start(reader.run)
            # Returned only because the reader signalled ready — and it is armed.
            assert coord._esc_reader is reader
            tg.cancel_scope.cancel()
    finally:
        os.close(master)
        with contextlib.suppress(OSError):
            os.close(slave)


@pytest.mark.anyio
async def test_run_task_start_returns_even_without_dev_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When there is no controlling tty (CI / piped stdin) the reader can't arm,
    but `tg.start` must still return — signalling ready on the unarmed path —
    rather than hang the run loop forever."""
    def _fail_open(path: str, flags: int, mode: int = 0o777) -> int:
        if path == "/dev/tty":
            raise OSError("no tty")
        return os.open(path, flags, mode)

    monkeypatch.setattr(os, "open", _fail_open)
    coord = InterruptCoordinator()
    reader = EscKeyReader(coord)
    async with anyio.create_task_group() as tg:
        await tg.start(reader.run)          # must not hang
        assert coord._esc_reader is None    # unarmed: no controlling tty
        tg.cancel_scope.cancel()


def test_read_one_byte_leaves_pending_byte_when_quiescing() -> None:
    """Regression: the prompt's first keystroke must not be stolen.

    When a prompt is quiescing (`_quiesce_depth > 0`) the reader may still have
    an in-flight read. A byte that lands in /dev/tty's buffer in that pre-park
    window belongs to prompt_toolkit, not the ESC reader. Consuming it here is
    the `ESC_READER_ATE: b'1'` race: prompt_toolkit then sees only the trailing
    `\\r`, reads empty input, and re-prompts forever (the e2e human-gate hang).
    """
    import pty
    import select
    import tty as ttymod

    master, slave = pty.openpty()
    try:
        ttymod.setcbreak(slave)              # the armed reader holds the tty in cbreak
        coord = InterruptCoordinator()
        reader = EscKeyReader(coord)
        reader._fd = slave
        reader._quiesce_depth = 1            # a prompt is quiescing
        os.write(master, b"1")               # the prompt's keystroke arrives
        for _ in range(50):                  # let the byte propagate to the slave
            r, _w, _e = select.select([slave], [], [], 0.02)
            if r:
                break
        got = reader._read_one_byte()
        assert got == b"", f"reader stole {got!r} destined for the prompt"
        # The byte survives in the tty buffer for prompt_toolkit to read.
        ready, _, _ = select.select([slave], [], [], 1.0)
        assert ready, "pending byte vanished from the tty buffer"
        assert os.read(slave, 1) == b"1"
    finally:
        os.close(master)
        with contextlib.suppress(OSError):
            os.close(slave)


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
