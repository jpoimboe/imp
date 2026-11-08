"""The `p` picker: the cbreak reader's binding, its refusals, the retry-backoff
gate on the seam, and the block that seam renders."""
from __future__ import annotations

import contextlib
import io
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import anyio
import pytest

from imp.cli.signals import CtrlDReader, InterruptCoordinator
from tests._event_fixtures import ev
from tests.async_wait import wait_until
from tests.test_signals import _can_pty, _patch_dev_tty


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# Only the reader-driven tests need a tty. The structural no-latch guard and the
# frame-send test must run everywhere — a guard that vanishes on a pty-less
# machine is the one that should always run.
_needs_pty = pytest.mark.skipif(not _can_pty(), reason="pty not available")


def _picker(choices: list[str | None], opened: list[int],
            *, gate: Callable[[], bool] = lambda: True) -> Any:
    @contextlib.contextmanager
    def _block():
        opened.append(1)
        yield choices

    def _open():
        return _block() if gate() else None

    return _open


async def _drive(monkeypatch, keys: bytes, *, bind_hook: bool,
                 choices: list[str | None],
                 gate: Callable[[], bool] = lambda: True):
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
        reader = CtrlDReader(coord, provider_picker=_picker(choices, opened, gate=gate))
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
    from imp.cli.session_run import build_provider_picker
    from tests.test_provider_reconcile import _seed

    quest, _state, _wf = _seed(tmp_path)
    floor_availability({"claude", "codex"})
    trace: list[str] = []

    class _Driver:
        def emit_lines(self, lines: list[str]) -> None:
            trace.append("emit")
            trace.extend(lines)

        def retry_countdown_in_flight(self) -> bool:
            return True

    class _Status:
        @contextlib.contextmanager
        def suspend(self):
            trace.append("enter")
            try:
                yield
            finally:
                trace.append("exit")

    opened = build_provider_picker(quest, _Driver(), _Status())()
    assert opened is not None
    with opened as choices:
        trace.append("read")

    assert choices == [None, "codex"]        # the resolved vendor arms nothing
    block = [
        "switch provider — the rest of this quest runs on your pick",
        "takes effect right away — this step is waiting to retry",
        "",
        "configured",
        "  1  claude   current",
        "also installed",
        "  2  codex",
        "",
        "press a number, or esc to cancel",
    ]
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


class _RecordingStatus:
    """Counts suspend brackets: a refused `p` must enter none at all."""

    def __init__(self) -> None:
        self.entered = 0

    @contextlib.contextmanager
    def suspend(self):
        self.entered += 1
        yield


def _gate_driver(retry: str | None) -> tuple[Any, io.StringIO]:
    """A real LiveDriver mid-step, its activity carrying a retry countdown when
    `retry` names a backoff category — the signal the gate reads."""
    from imp.cli.live_driver import LiveDriver
    from imp.cli.live_status import LiveStatus

    out = io.StringIO()
    drv = LiveDriver(stream=out, color=False, width=80, status=LiveStatus(out))
    drv.on_event(ev("1", "quest_created", quest_id=1, title="x"))
    drv.on_event(ev("2", "step_started", step="deferred-sweep", kind="agent",
                    phase="convergence", phase_label="convergence phase",
                    label="sweeping deferred tasks", model="claude"))
    if retry is not None:
        drv.on_event(ev("3", "agent_retry", category=retry, attempt=1,
                        backoff_seconds=300.0))
    return drv, out


def _quest(tmp_path: Path, floor_availability: Any,
           available: set[str] | None = None) -> Any:
    from tests.test_provider_reconcile import _seed

    quest, _state, _wf = _seed(tmp_path)
    floor_availability({"claude", "codex"} if available is None
                       else available)
    return quest


_PICKER_TOP = "switch provider — the rest of this quest runs on your pick"


# AC 7a, AC 7c
def test_the_seam_is_consulted_per_keypress_not_captured_at_construction(
    tmp_path: Path, floor_availability,
) -> None:
    # Production builds the seam ONCE, where the CtrlDReader starts — at session
    # start, when no backoff can be in flight. A gate read at construction and
    # closed over would leave `p` dead for the entire run, so one seam is built
    # up front here and consulted across the whole life of the wait.
    from imp.cli.session_run import build_provider_picker

    quest = _quest(tmp_path, floor_availability)
    drv, out = _gate_driver(None)
    status = _RecordingStatus()
    picker = build_provider_picker(quest, drv, status)

    assert picker() is None                 # before the wait
    assert status.entered == 0
    assert "switch provider" not in out.getvalue()

    drv.on_event(ev("3", "agent_retry", category="rate_limit", attempt=1,
                    backoff_seconds=300.0))
    opened = picker()                       # same seam object, now open
    assert opened is not None
    with opened as choices:
        assert choices == [None, "codex"]
    assert status.entered == 1
    assert _PICKER_TOP in out.getvalue()

    drv.on_event(ev("4", "backoff_ended", reason="elapsed"))
    assert picker() is None                 # the window closes with the wait
    assert status.entered == 1


# AC 7c, AC 7d
@pytest.mark.parametrize("category", ["rate_limit", "auth", "network", "stalled"])
def test_the_seam_opens_for_any_retry_backoff(
    tmp_path: Path, floor_availability, category: str,
) -> None:
    # Every is_retry_with_backoff category sets the countdown, so an auth or
    # network wait opens the picker exactly as a rate limit does.
    from imp.cli.session_run import build_provider_picker

    quest = _quest(tmp_path, floor_availability)
    drv, out = _gate_driver(None)
    status = _RecordingStatus()
    picker = build_provider_picker(quest, drv, status)
    assert picker() is None

    drv.on_event(ev("3", "agent_retry", category=category, attempt=1,
                    backoff_seconds=300.0))
    opened = picker()
    assert opened is not None
    with opened as choices:
        assert choices == [None, "codex"]
    assert status.entered == 1
    assert _PICKER_TOP in out.getvalue()


# AC 7e
def test_an_empty_roster_is_not_read_as_a_shut_gate(
    tmp_path: Path, floor_availability,
) -> None:
    # The conflation this guards against can only arise inside the seam, where
    # the roster is read: no selectable provider is still an OPEN picker that
    # renders and yields an empty choice list — never the gate's None.
    from imp.cli.session_run import build_provider_picker

    quest = _quest(tmp_path, floor_availability, available=set())
    drv, out = _gate_driver("rate_limit")
    status = _RecordingStatus()
    opened = build_provider_picker(quest, drv, status)()
    assert opened is not None
    with opened as choices:
        assert choices == []
    assert status.entered == 1
    assert _PICKER_TOP in out.getvalue()
    assert "press a number, or esc to cancel" in out.getvalue()


def test_the_seam_tags_the_pinned_vendor_as_current(
    tmp_path: Path, floor_availability,
) -> None:
    from imp.cli.session_run import build_provider_picker

    quest = _quest(tmp_path, floor_availability)
    quest.set_provider("codex")
    drv, out = _gate_driver("rate_limit")
    opened = build_provider_picker(quest, drv, _RecordingStatus())()
    assert opened is not None
    with opened as choices:
        assert choices == ["claude", None]
    assert "  2  codex   current" in out.getvalue()


def test_the_seam_numbers_in_the_quests_configured_order(
    tmp_path: Path, floor_availability,
) -> None:
    # The digit map must follow the displayed (configured) order, not floor order.
    from imp.cli.session_run import build_provider_picker
    from imp.paths import project_state_dir

    quest = _quest(tmp_path, floor_availability)
    (project_state_dir(tmp_path) / "config.yaml").write_text(
        "providers: [codex, claude]\n")
    drv, out = _gate_driver("rate_limit")
    opened = build_provider_picker(quest, drv, _RecordingStatus())()
    assert opened is not None
    with opened as choices:
        assert choices == ["codex", None]       # resolved claude is slot 2
    assert "also installed" not in out.getvalue()


@_needs_pty
@pytest.mark.anyio
async def test_p_outside_a_retry_backoff_opens_nothing(monkeypatch) -> None:
    # AC 7a: the seam hands back None, so the picker never opens and the digit
    # behind the p arms nothing.
    sent, opened = await _drive(monkeypatch, b"p2", bind_hook=True,
                                choices=[None, "codex"], gate=lambda: False)
    assert (sent, opened) == ([], 0)


@_needs_pty
@pytest.mark.anyio
async def test_the_key_after_a_refused_p_is_read_as_a_fresh_keypress(
    monkeypatch,
) -> None:
    # AC 7b: the shut gate consumes no byte — the second p (gate now open) opens
    # the picker and the digit behind it arms, which a swallowed byte could not.
    consults: list[int] = []

    def gate() -> bool:
        consults.append(1)
        return len(consults) > 1

    sent, opened = await _drive(monkeypatch, b"pp2", bind_hook=True,
                                choices=[None, "codex"], gate=gate)
    assert (sent, opened) == (["codex"], 1)
