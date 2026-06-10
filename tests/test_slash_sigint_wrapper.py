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
    async def ask_async(self, prompt_text=None):
        raise KeyboardInterrupt


class _FakeCoord:
    def __init__(self) -> None:
        self.exit_reason = None
        self._esc_reader = None
        self._exit_event = None

    def tty_quiesced(self):
        # Async context manager — mirrors InterruptCoordinator.tty_quiesced()
        @contextlib.asynccontextmanager
        async def _c():
            yield

        return _c()

    def rearm_signal_handlers(self) -> None: ...

    def request_exit(self, reason: str) -> None:
        self.exit_reason = reason


@pytest.mark.anyio
async def test_out_of_band_sigint_routes_to_exit(tmp_path: Path) -> None:
    coord = _FakeCoord()
    with pytest.raises(KeyboardInterrupt):
        await ask_line_with_slash(
            _KbiPrompt(), _FakeStatus(), _fake_change(tmp_path), coord=coord)
    assert coord.exit_reason == "sigint"
