from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import anyio
import pytest

from imp.agents.agent_loop import run_agent_loop
from imp.agents.loop_items import SaveRequested
from imp.cli.signals import InterruptCoordinator
from imp.providers.observe_session import ObserveSession
from tests.async_wait import HANG_BACKSTOP


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _SaveHook:
    """Records each save request and cancels the run, as CONTROL_SAVE does."""

    def __init__(self) -> None:
        self.calls: list[str | None] = []
        self.scope = anyio.CancelScope()

    def __call__(self, reason: str | None) -> None:
        self.calls.append(reason)
        self.scope.cancel()


def _coordinator(hook: _SaveHook) -> InterruptCoordinator:
    coord = InterruptCoordinator()
    coord.set_save_hook(hook)
    return coord


class _SavingObserve(ObserveSession):
    """Its first raw message logs a save_and_exit result; later output must stay unread."""

    def __init__(self, coordinator: Any) -> None:
        super().__init__(SimpleNamespace(), SimpleNamespace(_cancel=False))
        self.ctx = SimpleNamespace(coordinator=coordinator, dependency_wait=())
        self.consumed: list[str] = []

    @property
    def _save_result_logged(self) -> bool:
        return bool(self.consumed)

    async def _enter(self) -> None:
        pass

    async def _receive(self) -> Any:
        for raw in ("save_and_exit result", "later model output"):
            yield raw

    async def _message_to_items(self, raw: Any) -> Any:
        self.consumed.append(raw)
        return
        yield  # pragma: no cover — makes this an async generator

    def _drain_kills(self) -> list[dict[str, Any]]:
        return []

    def _fatal_error(self) -> dict[str, Any] | None:
        return None

    async def _is_terminal(self, raw: Any) -> bool:
        raise AssertionError("the save checkpoint must precede the terminal check")


@pytest.mark.anyio
async def test_observe_events_stop_at_the_logged_save_result() -> None:
    session = _SavingObserve(coordinator=None)
    items = [item async for item in session.events()]
    assert items == [SaveRequested()]
    assert session.consumed == ["save_and_exit result"]


@pytest.mark.anyio
async def test_observe_checkpoint_requests_one_save_then_parks() -> None:
    hook = _SaveHook()
    session = _SavingObserve(_coordinator(hook))
    continued: list[str] = []

    def _continue(last_text: str) -> tuple[str, bool]:
        continued.append(last_text)
        hook.scope.cancel()  # a missed checkpoint fails fast instead of hanging
        return ("keep going", False)

    yielded: list[dict[str, Any]] = []
    with anyio.fail_after(HANG_BACKSTOP), hook.scope:
        async for ev in run_agent_loop(
                SimpleNamespace(continuation_fn=_continue), session):
            yielded.append(ev)
    assert hook.scope.cancelled_caught
    assert hook.calls == [None]
    assert yielded == []
    assert continued == []
