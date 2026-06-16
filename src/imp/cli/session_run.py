from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import anyio

from imp.cli.session_terminal import run_terminal_client
from imp.cli.signals import InterruptCoordinator
from imp.engine.change import Change
from imp.engine.executor import RunResult
from imp.engine.state import State
from imp.remote.session.attach import build_session_attach


def infer_session_result(change: Change) -> RunResult:
    """Infer RunResult from disk: no worktree or completed status → COMPLETED, else PAUSED."""
    worktree = change.control_root / ".imp" / "worktrees" / change.id
    if not worktree.exists():
        return RunResult.COMPLETED
    if State.load(change.state_dir).status == "completed":
        return RunResult.COMPLETED
    return RunResult.PAUSED


async def run_session_client(
    change: Change,
    *,
    driver: Any,
    coord: InterruptCoordinator,
    solicit: Callable[[str], Awaitable[str]],
    seed_opening: str | None = None,
    status: Any = None,
) -> int:
    """Attach to the session daemon, render events, answer gates, and return the exit code."""
    attach = build_session_attach(change, seed_opening=seed_opening)
    async with anyio.create_task_group() as tg:
        tg.start_soon(coord.run_signal_receiver)
        if status is not None:
            tg.start_soon(status.tick)
        await run_terminal_client(
            attach=attach, driver=driver, coord=coord, solicit=solicit)
        tg.cancel_scope.cancel()
    # Lazy import: the CLI-flip plan makes imp.cli.main import this module, so a
    # module-level import of _handle_terminal_outcome would be circular.
    from imp.cli.main import _handle_terminal_outcome
    return _handle_terminal_outcome(change, infer_session_result(change), coord)
