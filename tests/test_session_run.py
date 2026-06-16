from __future__ import annotations

from pathlib import Path
from typing import Any

import anyio
import pytest

from imp.cli.session_run import (
    infer_session_result,
    make_headless_solicit,
    run_session_client,
)
from imp.cli.signals import InterruptCoordinator
from imp.engine.change import Change
from imp.engine.executor import RunResult
from imp.engine.state import State
from imp.remote.session.discover import session_socket_path
from imp.remote.session.server import SessionServer


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _make_change(tmp_path: Path, change_id: str) -> Change:
    return Change(
        id=change_id,
        title="T",
        base_branch="main",
        control_root=tmp_path,
        project_root=tmp_path,
        change_dir=tmp_path / ".imp" / "change",
    )


def test_infer_session_result_completed_when_worktree_removed(tmp_path: Path) -> None:
    # A merge-completing run removes the worktree (and the state dir inside it).
    change = _make_change(tmp_path, "2026-06-15-merged")
    assert infer_session_result(change) is RunResult.COMPLETED


def test_infer_session_result_completed_when_status_completed(tmp_path: Path) -> None:
    change = _make_change(tmp_path, "2026-06-15-done")
    (tmp_path / ".imp" / "worktrees" / change.id).mkdir(parents=True)
    State(id=change.id, step=None, step_attempt=0, status="completed").save(
        change.state_dir)
    assert infer_session_result(change) is RunResult.COMPLETED


def test_infer_session_result_paused_when_parked_at_gate(tmp_path: Path) -> None:
    change = _make_change(tmp_path, "2026-06-15-parked")
    (tmp_path / ".imp" / "worktrees" / change.id).mkdir(parents=True)
    State(id=change.id, step="plan", step_attempt=0, status="paused").save(
        change.state_dir)
    assert infer_session_result(change) is RunResult.PAUSED


def test_infer_session_result_paused_when_merge_blocked(tmp_path: Path) -> None:
    change = _make_change(tmp_path, "2026-06-15-blocked")
    (tmp_path / ".imp" / "worktrees" / change.id).mkdir(parents=True)
    State(id=change.id, step=None, step_attempt=0, status="merge_blocked").save(
        change.state_dir)
    assert infer_session_result(change) is RunResult.PAUSED


@pytest.mark.anyio
async def test_run_session_client_answers_gate_and_returns_completed_rc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    change = _make_change(tmp_path, "2026-06-15-run")
    # The on-disk terminal state the client reads once the daemon exits.
    (tmp_path / ".imp" / "worktrees" / change.id).mkdir(parents=True)
    State(id=change.id, step=None, step_attempt=0, status="completed").save(
        change.state_dir)

    # No real subprocess: in-process server stands in; spawn is a no-op.
    monkeypatch.setattr(
        "imp.remote.session.attach.spawn_session_daemon",
        lambda ch, *, seed_opening=None: None)

    sock = str(session_socket_path(change.control_root, change.id))
    server = SessionServer(sock, hibernate=False)
    answered: list[str] = []

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        with anyio.fail_after(10):
            while server.attached_count == 0:
                await anyio.sleep(0.02)
        on_event({"type": "awaiting_user_input", "payload": {"prompt": "name?"}})
        answered.append(await user_chat_input_fn("name?"))
        return "completed"

    class _Driver:
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...

    async def _solicit(_prompt: str) -> str:
        return "imp"

    coord = InterruptCoordinator()
    rc_holder: dict[str, int] = {}
    with anyio.fail_after(30):
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _go() -> None:
                rc_holder["rc"] = await run_session_client(
                    change, driver=_Driver(), coord=coord, solicit=_solicit)

            tg.start_soon(_go)
            with anyio.fail_after(15):
                while "rc" not in rc_holder:
                    await anyio.sleep(0.02)
            tg.cancel_scope.cancel()

    assert answered == ["imp"]
    assert rc_holder["rc"] == 0


@pytest.mark.anyio
async def test_run_session_client_starts_status_tick_when_provided(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    change = _make_change(tmp_path, "2026-06-15-tick")
    (tmp_path / ".imp" / "worktrees" / change.id).mkdir(parents=True)
    State(id=change.id, step=None, step_attempt=0, status="completed").save(
        change.state_dir)

    monkeypatch.setattr(
        "imp.remote.session.attach.spawn_session_daemon",
        lambda ch, *, seed_opening=None: None)

    sock = str(session_socket_path(change.control_root, change.id))
    server = SessionServer(sock, hibernate=False)
    tick_started = anyio.Event()

    class _Status:
        async def tick(self) -> None:
            tick_started.set()
            await anyio.sleep_forever()

    class _Driver:
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        with anyio.fail_after(10):
            while server.attached_count == 0:
                await anyio.sleep(0.02)
        return "completed"

    async def _solicit(_prompt: str) -> str:  # pragma: no cover
        return ""

    coord = InterruptCoordinator()
    rc_holder: dict[str, int] = {}
    with anyio.fail_after(30):
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _go() -> None:
                rc_holder["rc"] = await run_session_client(
                    change, driver=_Driver(), coord=coord,
                    solicit=_solicit, status=_Status())

            tg.start_soon(_go)
            with anyio.fail_after(15):
                while "rc" not in rc_holder:
                    await anyio.sleep(0.02)
            tg.cancel_scope.cancel()

    assert tick_started.is_set()
    assert rc_holder["rc"] == 0


@pytest.mark.anyio
async def test_make_headless_solicit_delivers_message_once_then_signals_detach() -> None:
    coord = InterruptCoordinator()
    coord.escalation_event = anyio.Event()  # run_terminal_client sets this before any gate
    solicit = make_headless_solicit("ship it", coord)

    # First gate: the single --message is handed over; no detach yet.
    assert await solicit("first gate?") == "ship it"
    assert not coord.escalation_event.is_set()

    # Second gate: no more input — the solicit must NOT return a (stray) reply; it
    # signals a clean detach and blocks. move_on_after fires only because it blocked.
    with anyio.move_on_after(0.2) as scope:
        await solicit("second gate?")
    assert scope.cancelled_caught
    assert coord.escalation_event.is_set()


@pytest.mark.anyio
async def test_make_headless_solicit_with_no_message_signals_detach_immediately() -> None:
    coord = InterruptCoordinator()
    coord.escalation_event = anyio.Event()
    solicit = make_headless_solicit(None, coord)

    # No message at all (e.g. a resume parked at a gate with nothing to inject):
    # the very first gate signals a detach and blocks.
    with anyio.move_on_after(0.2) as scope:
        await solicit("gate?")
    assert scope.cancelled_caught
    assert coord.escalation_event.is_set()
