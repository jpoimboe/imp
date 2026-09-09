"""The `p` picker: the cbreak reader's binding, its refusals, and the seam that
renders it (Scope 3; AC 8a, 8b, 8c, 8d, 8f)."""
from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import Any

import anyio
import pytest

from imp.cli.signals import CtrlDReader, InterruptCoordinator
from tests.async_wait import wait_until
from tests.test_signals import _can_pty, _patch_dev_tty


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# Only the reader-driven tests need a tty. The structural no-latch guard and the
# frame-send test must run everywhere — a guard that vanishes on a pty-less
# machine is the one that should always run.
_needs_pty = pytest.mark.skipif(not _can_pty(), reason="pty not available")


def _picker(choices: list[str | None], opened: list[int]) -> Any:
    @contextlib.contextmanager
    def _open():
        opened.append(1)
        yield choices
    return _open


async def _drive(monkeypatch, keys: bytes, *, bind_hook: bool,
                 choices: list[str | None]):
    """Feed KEYS to a live reader; return (dispatched names, picker-open count)."""
    import pty
    sent: list[str] = []
    opened: list[int] = []
    master, slave = pty.openpty()
    try:
        _patch_dev_tty(monkeypatch, slave)
        coord = InterruptCoordinator()
        coord.escalation_event = anyio.Event()
        if bind_hook:
            async def _fwd(name: str) -> None:
                sent.append(name)
            coord.set_provider_hook(_fwd)
        reader = CtrlDReader(coord, provider_picker=_picker(choices, opened))
        async with anyio.create_task_group() as tg:
            await tg.start(reader.run)
            os.write(master, keys)
            # A trailing ctrl-d is the settle signal: it can only be read after
            # every earlier byte was, so no fixed sleep is needed.
            os.write(master, b"\x04")
            await wait_until(lambda: coord.should_pause)
            tg.cancel_scope.cancel()
    finally:
        os.close(master)
        with contextlib.suppress(OSError):
            os.close(slave)
    return sent, len(opened)


@_needs_pty
@pytest.mark.anyio
async def test_p_then_a_digit_arms_the_named_provider(monkeypatch) -> None:
    sent, opened = await _drive(monkeypatch, b"p2", bind_hook=True,
                                choices=[None, "codex"])
    assert (sent, opened) == (["codex"], 1)


@_needs_pty
@pytest.mark.anyio
async def test_esc_cancels_the_picker_and_the_reader_keeps_watching(
    monkeypatch,
) -> None:
    sent, opened = await _drive(monkeypatch, b"p\x1b", bind_hook=True,
                                choices=[None, "codex"])
    assert (sent, opened) == ([], 1)


@_needs_pty
@pytest.mark.anyio
async def test_the_current_vendors_digit_arms_nothing(monkeypatch) -> None:
    # AC 8d: its slot carries no name, so no pin is written and no marker is
    # broadcast that no reconcile would clear.
    sent, opened = await _drive(monkeypatch, b"p1", bind_hook=True,
                                choices=[None, "codex"])
    assert (sent, opened) == ([], 1)


@_needs_pty
@pytest.mark.anyio
async def test_a_digit_past_the_roster_arms_nothing(monkeypatch) -> None:
    sent, opened = await _drive(monkeypatch, b"p9", bind_hook=True,
                                choices=[None, "codex"])
    assert (sent, opened) == ([], 1)


@_needs_pty
@pytest.mark.anyio
async def test_p_before_the_hook_is_bound_never_opens_the_picker(
    monkeypatch,
) -> None:
    # AC 8f: refused, not swallowed — the picker does not open, so no digit is
    # read into it and nothing is armed.
    sent, opened = await _drive(monkeypatch, b"p2", bind_hook=False,
                                choices=[None, "codex"])
    assert (sent, opened) == ([], 0)


@_needs_pty
@pytest.mark.anyio
async def test_ctrl_d_inside_the_picker_still_leaves_the_run(monkeypatch) -> None:
    # The reader's invariant: \x04 leaves, even when the picker is the byte's
    # reader — _drive only returns once the coordinator was asked to pause.
    sent, opened = await _drive(monkeypatch, b"p", bind_hook=True,
                                choices=[None, "codex"])
    assert (sent, opened) == ([], 1)


@pytest.mark.anyio
async def test_the_picker_reports_the_ctrl_d_so_the_reader_leaves(
    monkeypatch,
) -> None:
    # The other half of the \x04 branch: a False return is what makes the outer
    # read loop return — leaving the run rather than reading on.
    sent: list[str] = []
    opened: list[int] = []
    coord = InterruptCoordinator()
    coord.escalation_event = anyio.Event()

    async def _fwd(name: str) -> None:
        sent.append(name)

    coord.set_provider_hook(_fwd)
    reader = CtrlDReader(coord, provider_picker=_picker([None, "codex"], opened))
    monkeypatch.setattr(reader, "_read_one_byte", lambda: b"\x04")
    assert await reader._pick_provider() is False
    assert (sent, len(opened)) == ([], 1)


def test_the_seam_renders_the_block_inside_a_suspend_bracket(
    tmp_path: Path, floor_availability,
) -> None:
    # The 100ms footer tick would paint over an unsuspended block, and the
    # suspend must still be held when the digit is read.
    from imp.cli.provider_command import render_picker
    from imp.cli.session_run import build_provider_picker
    from tests.test_provider_reconcile import _seed

    quest, _state, _wf = _seed(tmp_path)
    floor_availability({"claude_code", "codex"})
    trace: list[str] = []

    class _Driver:
        def emit_lines(self, lines: list[str]) -> None:
            trace.append("emit")
            trace.extend(lines)

    class _Status:
        @contextlib.contextmanager
        def suspend(self):
            trace.append("enter")
            try:
                yield
            finally:
                trace.append("exit")

    with build_provider_picker(quest, _Driver(), _Status())() as choices:
        trace.append("read")

    assert choices == [None, "codex"]        # the resolved vendor arms nothing
    block = render_picker("claude_code", ["claude_code", "codex"])
    assert trace == ["enter", "emit", *block, "read", "exit"]


def test_the_coordinator_holds_no_provider_latch() -> None:
    # AC 8d: the durable pin is the only state; a second in-memory copy would
    # need its own set/clear/lifetime rules. _backoff_wait_depth is untouched.
    coord = InterruptCoordinator()
    assert coord.provider_hook_bound is False
    src = Path("src/imp/cli/signals.py").read_text()
    assert "_pending_provider" not in src
    assert "provider" not in src.split("def request_retry_now")[1].split("def ")[0]


@pytest.mark.anyio
async def test_the_bound_hook_sends_a_set_provider_frame() -> None:
    # AC 8b/8c: the gesture leaves the client as CONTROL_SET_PROVIDER, which is
    # what the daemon turns into the durable quest.json write.
    from imp.cli.session_controls import SessionControls
    from imp.remote.session.protocol import CONTROL_SET_PROVIDER

    frames: list[tuple[str, str | None]] = []

    class _Client:
        async def send_control(self, action: str, reason: str | None = None) -> None:
            frames.append((action, reason))

    coord = InterruptCoordinator()
    SessionControls(_Client(), coord)          # binds the hook
    assert coord.provider_hook_bound is True
    await coord.dispatch_set_provider("codex")
    assert frames == [(CONTROL_SET_PROVIDER, "codex")]
