# tests/test_slash_sigint_wrapper.py
from __future__ import annotations

import contextlib
from pathlib import Path

import pytest

from imp.cli.slash import ask_line_with_slash


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _fake_change(tmp_path: Path):
    """Minimal Change stand-in: ask_line_with_slash needs .workflow_dir (for
    handle_slash_line) and .event_log().append(...) (for skill_load events).
    Mirrors the _FakeChange from test_slash.py."""
    from imp.engine.state import JsonlEventLog

    class _FC:
        def __init__(self) -> None:
            self.workflow_dir = tmp_path / ".imp" / "change" / "workflow"
            skills = self.workflow_dir / "skills"
            skills.mkdir(parents=True)
            self._events = tmp_path / "events.jsonl"

        def event_log(self):
            return JsonlEventLog(self._events)

    return _FC()


class _FakeStatus:
    def suspend(self):
        return contextlib.nullcontext()


class _KbiPrompt:
    async def ask_async(
        self, prompt_text=None, *, question: str = "", width: int = 80, indent: str = "",
        bottom_toolbar=None, hint=None
    ) -> str:
        raise KeyboardInterrupt


class _FakeCoord:
    def __init__(self) -> None:
        self.exit_reason = None
        self._exit_event = None
        self.at_prompt_calls: list[bool] = []

    def set_at_prompt(self, value: bool) -> None:
        self.at_prompt_calls.append(value)

    def rearm_signal_handlers(self) -> None: ...

    def request_exit(self, reason: str) -> None:
        self.exit_reason = reason


@pytest.mark.anyio
async def test_out_of_band_sigint_routes_to_exit(tmp_path: Path) -> None:
    coord = _FakeCoord()
    with pytest.raises(KeyboardInterrupt):
        await ask_line_with_slash(
            _KbiPrompt(), _FakeStatus(), _fake_change(tmp_path), coord=coord)
    assert coord.exit_reason == "ctrl-c"


@pytest.mark.anyio
async def test_out_of_band_sigint_reraises_without_coord(tmp_path: Path) -> None:
    # coord=None (non-interactive): the KBI must still propagate (the CLI arm
    # finalizes), and the guard must not crash trying to call request_exit.
    with pytest.raises(KeyboardInterrupt):
        await ask_line_with_slash(
            _KbiPrompt(), _FakeStatus(), _fake_change(tmp_path), coord=None)


class _LinePrompt:
    async def ask_async(
        self, prompt_text=None, *, question: str = "", width: int = 80, indent: str = "",
        bottom_toolbar=None, hint=None
    ) -> str:
        return "hello"


class _EofPrompt:
    async def ask_async(
        self, prompt_text=None, *, question: str = "", width: int = 80, indent: str = "",
        bottom_toolbar=None, hint=None
    ) -> str:
        raise EOFError


@pytest.mark.anyio
async def test_at_prompt_set_then_reset_on_normal_return(tmp_path: Path) -> None:
    # _at_prompt must be True for the prompt turn and reset to False on a normal
    # return so a SIGINT during a subsequent RUN takes the signal_pause path.
    coord = _FakeCoord()
    line = await ask_line_with_slash(
        _LinePrompt(), _FakeStatus(), _fake_change(tmp_path), coord=coord)
    assert line == "hello"
    assert coord.at_prompt_calls == [True, False]   # set entering, reset in finally


@pytest.mark.anyio
async def test_at_prompt_reset_on_exception_path(tmp_path: Path) -> None:
    # Even when the prompt raises (EOFError on Ctrl-D), the finally must reset
    # _at_prompt to False — otherwise a subsequent mid-run SIGINT would wrongly
    # route to exit instead of pausing the run.
    coord = _FakeCoord()
    with pytest.raises(EOFError):
        await ask_line_with_slash(
            _EofPrompt(), _FakeStatus(), _fake_change(tmp_path), coord=coord)
    assert coord.at_prompt_calls == [True, False]
