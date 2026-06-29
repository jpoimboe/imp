# tests/test_prompt_teardown_on_exit.py
"""Task D3 — an OPEN prompt tears down when the coordinator exit event fires
(SIGTERM / escalation / out-of-band SIGINT), and a bare-turn-end solicit
cancellation converts into AwaitingUserInput (its existing headless path).

The M2 verification tests (test_real_prompt_* and test_solicit_cancel_*) drive a
REAL prompt_toolkit session — sleep_forever does NOT exercise prompt_toolkit's
app-teardown-under-cancel, which is exactly where the anyio cancellation hazard
bites. They assert NO "cancel scope ... was not exited in order" message is
emitted (captured via warnings-as-errors + stderr inspection)."""

from __future__ import annotations

import contextlib
import io
import warnings
from contextlib import redirect_stderr
from pathlib import Path

import anyio
import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from imp.cli.prompt import Prompt
from imp.cli.slash import _ask_or_exit, ask_line_with_slash
from imp.engine.awaiting_user_input import AwaitingUserInput


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _FakeStatus:
    def suspend(self):
        return contextlib.nullcontext()


class _BlockingPrompt:
    async def ask_async(self, *_a, **_k):
        await anyio.sleep_forever()  # an open prompt, never submitted


def _fake_change(tmp_path: Path):
    from imp.engine.state import JsonlEventLog

    class _FC:
        def __init__(self) -> None:
            self.workflow_dir = tmp_path / ".imp" / "change" / "workflow"
            (self.workflow_dir / "skills").mkdir(parents=True)
            self._events = tmp_path / "events.jsonl"

        def event_log(self):
            return JsonlEventLog(self._events)

    return _FC()


class _FakeCoord:
    def __init__(self, exit_event) -> None:
        self._exit_event = exit_event
        self.exit_reason = None
        self.at_prompt_calls: list[bool] = []

    def set_at_prompt(self, value: bool) -> None:
        self.at_prompt_calls.append(value)

    def rearm_signal_handlers(self) -> None: ...

    def request_exit(self, reason: str) -> None:
        self.exit_reason = reason


# ── Step 1 sketch: open prompt cancelled by the exit event ───────────────────


def _fire_soon(exit_event: anyio.Event, delay: float = 0.05) -> None:
    """Schedule exit_event.set() on the loop WITHOUT a sibling task group.

    The plan sketch raced `fire` against the prompt inside an OUTER task group,
    but anyio wraps any exception leaving that group in a BaseExceptionGroup —
    obscuring the single KeyboardInterrupt/AwaitingUserInput the call raises.
    Firing via loop.call_later lets the function-under-test be awaited directly,
    so its exception propagates UNWRAPPED. The exit event is itself loop-safe
    (anyio.Event.set is sync and thread-confined to the loop here)."""
    import asyncio

    asyncio.get_running_loop().call_later(delay, exit_event.set)


@pytest.mark.anyio
async def test_exit_event_cancels_open_prompt(tmp_path: Path) -> None:
    exit_event = anyio.Event()
    coord = _FakeCoord(exit_event)
    _fire_soon(exit_event)

    with pytest.raises(KeyboardInterrupt):
        await ask_line_with_slash(
            _BlockingPrompt(), _FakeStatus(), _fake_change(tmp_path), coord=coord)
    # The synthesized KBI routes through D2's catch → request_exit (the
    # pause-and-exit handoff the CLI arm finalizes). The reason is "ctrl-c" to
    # match _reason_for_signal(SIGINT) and the _dispatch_signal exit routing, so
    # the final headline reads `paused (ctrl-c)` consistently.
    assert coord.exit_reason == "ctrl-c"


@pytest.mark.anyio
async def test_ask_or_exit_unwraps_prompt_exception_to_bare() -> None:
    # With the exit event set (post-C4) the read runs INSIDE _ask_or_exit's task
    # group, so any exception the prompt raises (EOFError on Ctrl-D,
    # KeyboardInterrupt on an out-of-band SIGINT) is wrapped by anyio in a
    # BaseExceptionGroup. _ask_or_exit must unwrap a lone such exception to BARE
    # so the call-site `except EOFError` / `except KeyboardInterrupt` arms keep
    # working. EOFError exercises the unwrap; the KeyboardInterrupt path shares
    # the IDENTICAL code (raising a real KBI from a coroutine is hostile to the
    # asyncio test runner, so it is covered by the manual anyio.run check, not a
    # live pytest case).
    class _EofPrompt:
        async def ask_async(self, *_a, **_k):
            raise EOFError

    exit_event = anyio.Event()  # never set; the prompt's own exception is the path
    with pytest.raises(EOFError):
        await _ask_or_exit(_EofPrompt(), exit_event)


@pytest.mark.anyio
async def test_no_exit_event_leaves_prompt_intact(tmp_path: Path) -> None:
    # Defensive: when the coordinator has no exit event (the C4 wiring not yet
    # present), the read takes the unraced path and a normal line returns.
    class _LinePrompt:
        async def ask_async(self, *_a, **_k):
            return "hello"

    coord = _FakeCoord(None)
    line = await ask_line_with_slash(
        _LinePrompt(), _FakeStatus(), _fake_change(tmp_path), coord=coord)
    assert line == "hello"


# ── M2 (a): REAL prompt_toolkit teardown under exit-event cancellation ───────


def _no_cancel_scope_warning(stderr_text: str) -> None:
    assert "cancel scope" not in stderr_text.lower(), (
        f"anyio scope-ordering violation in stderr:\n{stderr_text}")


@pytest.mark.anyio
async def test_real_prompt_torn_down_cleanly_by_exit_event() -> None:
    """M2 (a): wrap a REAL prompt_async in _ask_or_exit, fire the exit event
    mid-read, and assert it raises KeyboardInterrupt cleanly with NO 'cancel
    scope was not exited in order' message (the prompt_toolkit app teardown is
    what exercises the anyio hazard; sleep_forever does not)."""
    exit_event = anyio.Event()
    buf = io.StringIO()

    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        prompt = Prompt()  # real PromptSession under a pipe input/DummyOutput
        _fire_soon(exit_event)
        with warnings.catch_warnings():
            warnings.simplefilter("error")  # any anyio scope warning → exception
            with redirect_stderr(buf), pytest.raises(KeyboardInterrupt):
                # never submit a line: the open prompt blocks until the
                # exit event cancels the inner scope.
                await _ask_or_exit(prompt, exit_event)

    _no_cancel_scope_warning(buf.getvalue())


@pytest.mark.anyio
async def test_real_prompt_normal_submit_returns_line() -> None:
    """Control: when the line IS submitted before the exit event fires, the
    raced read returns the line and never raises."""
    exit_event = anyio.Event()
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        prompt = Prompt()
        pipe.send_text("hello\r")
        line = await _ask_or_exit(prompt, exit_event)
    assert line == "hello"


# ── M2 (b): REAL solicit cancellation → AwaitingUserInput, clean state ───────


@pytest.mark.anyio
async def test_solicit_cancel_converts_to_awaiting_user_input_cleanly() -> None:
    """M2 (b): a REAL prompt_async-backed user_input_fn is cancelled mid-solicit
    (the exit-event race tears down the prompt scope); build_continuation_fn's
    solicit converts the cancellation into AwaitingUserInput, leaving a clean
    resumable state with NO 'cancel scope not exited in order' warning."""
    from imp.engine.completion import build_continuation_fn

    buf = io.StringIO()

    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        prompt = Prompt()
        exit_event = anyio.Event()

        # The attended user_input_fn solicits through _ask_or_exit, so a fired
        # exit event tears the LOCAL prompt scope down (raising KeyboardInterrupt
        # out of _ask_or_exit) exactly as the CLI wiring will.
        async def user_input_fn(_prompt: str) -> str:
            return await _ask_or_exit(prompt, exit_event)

        cont = build_continuation_fn(
            instruction="finish.", user_input_fn=user_input_fn,
            coordinator=None, event_log=None, agent_id=None)
        _fire_soon(exit_event)

        with warnings.catch_warnings():
            warnings.simplefilter("error")
            with redirect_stderr(buf), pytest.raises(AwaitingUserInput) as ei:
                await cont("")

    # The cancellation converts cleanly; the pause carries no prompt.
    assert ei.value.prompt == ""
    _no_cancel_scope_warning(buf.getvalue())


@pytest.mark.anyio
async def test_solicit_normal_reply_unchanged() -> None:
    """Regression: the normal attended-reply path is untouched — a submitted
    line is returned as the next-message reply, no AwaitingUserInput."""
    from imp.engine.completion import build_continuation_fn

    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        prompt = Prompt()
        exit_event = anyio.Event()

        async def user_input_fn(_prompt: str) -> str:
            return await _ask_or_exit(prompt, exit_event)

        cont = build_continuation_fn(
            instruction="finish.", user_input_fn=user_input_fn,
            coordinator=None, event_log=None, agent_id=None)
        pipe.send_text("do the thing\r")
        reply = await cont("")
    assert reply == "do the thing"
