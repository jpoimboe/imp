from __future__ import annotations

import contextlib
import sys
from collections.abc import AsyncIterator, Iterator
from typing import Any

import anyio
import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from imp.cli import session_run
from imp.cli.session_run import make_interactive_confirm


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _Framing:
    """Stands in for coord, status and driver at once, recording the hand-off in order."""

    def __init__(self) -> None:
        self.events: list[str] = []

    def set_at_prompt(self, value: bool) -> None:
        self.events.append(f"at_prompt={value}")

    @contextlib.asynccontextmanager
    async def tty_quiesced(self) -> AsyncIterator[None]:
        self.events.append("quiesce")
        try:
            yield
        finally:
            self.events.append("unquiesce")

    def rearm_signal_handlers(self) -> None:
        self.events.append("rearm")

    @contextlib.contextmanager
    def suspend(self) -> Iterator[None]:
        self.events.append("suspend")
        try:
            yield
        finally:
            self.events.append("unsuspend")

    def prepare_for_prompt(self) -> None:
        self.events.append("prepare")

    def flush_deferred(self) -> None:
        self.events.append("flush")


def _confirm_for(f: _Framing) -> Any:
    return make_interactive_confirm(status=f, driver=f, coord=f)


_HANDOFF_OPEN = ["at_prompt=True", "quiesce", "suspend", "prepare"]
_HANDOFF_CLOSE = ["flush", "unsuspend", "unquiesce", "at_prompt=False", "rearm"]


@pytest.mark.anyio
@pytest.mark.parametrize("answer", [True, False])
async def test_confirm_hands_the_terminal_to_the_widget_and_back(
    monkeypatch: pytest.MonkeyPatch, answer: bool,
) -> None:
    f = _Framing()

    async def _widget(question: str) -> bool:
        f.events.append(f"ask {question}")
        return answer

    monkeypatch.setattr(session_run, "ask_keypress_confirm", _widget)
    assert await _confirm_for(f)("Abandon x? [y/N] ") is answer
    assert f.events == [*_HANDOFF_OPEN, "ask Abandon x? [y/N] ", *_HANDOFF_CLOSE]


@pytest.mark.anyio
async def test_cancelled_confirm_still_hands_the_terminal_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    f = _Framing()

    async def _widget(_question: str) -> bool:
        f.events.append("ask")
        await anyio.sleep_forever()
        return True   # unreachable: only cancellation ends it

    monkeypatch.setattr(session_run, "ask_keypress_confirm", _widget)
    with anyio.CancelScope() as scope:
        scope.cancel()
        await _confirm_for(f)("Q? ")
    assert f.events == [*_HANDOFF_OPEN, "ask", *_HANDOFF_CLOSE]


@pytest.mark.anyio
async def test_confirm_flushes_tty_stdin_before_the_widget_reads(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    import termios

    class _TtyStdin:
        def isatty(self) -> bool:
            return True

        def fileno(self) -> int:
            return 0

    flushed: list[tuple[int, int]] = []
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        def _fake_tcflush(fd: int, queue: int) -> None:
            flushed.append((fd, queue))
            pipe.send_text("y")   # the only key arrives after the flush

        monkeypatch.setattr(sys, "stdin", _TtyStdin())
        monkeypatch.setattr(termios, "tcflush", _fake_tcflush)
        with anyio.fail_after(10):
            assert await _confirm_for(_Framing())("Abandon x? [y/N] ") is True
    assert flushed == [(0, termios.TCIFLUSH)]
    assert capsys.readouterr().out == "Abandon x? [y/N] y\n"
