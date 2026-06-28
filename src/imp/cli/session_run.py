from __future__ import annotations

import sys
from collections.abc import Awaitable, Callable
from typing import Any

import anyio

from imp.cli.palette import should_use_color
from imp.cli.prompt import Prompt
from imp.cli.session_terminal import run_terminal_client
from imp.cli.signals import InterruptCoordinator
from imp.cli.slash import ask_line_with_slash
from imp.cli.terminal_title import TerminalTitle
from imp.engine.change import Change
from imp.engine.executor import RunResult
from imp.engine.state import State
from imp.remote.session.attach import build_session_attach


def infer_session_result(change: Change) -> RunResult:
    """Read persisted state: 'completed' → COMPLETED, else PAUSED."""
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
    resume: bool = False,
    opening_intent: str | None = None,
    leave_mode: str | None = None,
    leave_state: dict[str, str] | None = None,
    title: TerminalTitle | None = None,
    non_interactive: bool = False,
    autonomous: bool = False,
) -> int:
    """Attach to the session daemon, render events, answer gates, and return the exit code."""
    leave_state = {} if leave_state is None else leave_state
    title = title if title is not None else TerminalTitle(sys.stdout, change.id, change.title)
    attach = build_session_attach(
        change, seed_opening=seed_opening, resume=resume,
        opening_intent=opening_intent,
        non_interactive=non_interactive, autonomous=autonomous, lease=True)
    startup_failed = False
    async with anyio.create_task_group() as tg:
        tg.start_soon(coord.run_signal_receiver)
        if status is not None:
            tg.start_soon(status.tick)
        try:
            await run_terminal_client(
                attach=attach, driver=driver, coord=coord, solicit=solicit,
                change_id=change.id, control_root=change.control_root,
                leave_state=leave_state, leave_mode=leave_mode, title=title)
        except TimeoutError:
            startup_failed = True
            # attach raised before run_terminal_client's teardown finally runs.
            title.reset()
        tg.cancel_scope.cancel()
    if startup_failed and infer_session_result(change) is not RunResult.COMPLETED:
        print(
            f"imp: session daemon for {change.id} did not come up; "
            f"it may have crashed (see {change.control_root}/.imp/remote-control.log)",
            file=sys.stderr)
        return 3
    from imp.cli.main import (
        _print_leave_disposition,
        _session_exit_code,
    )
    disposition_rc = _print_leave_disposition(change, leave_state)
    if disposition_rc is not None:
        return disposition_rc
    return _session_exit_code(change, coord)


def make_headless_solicit(
    message: str | None, coord: InterruptCoordinator,
) -> Callable[[str], Awaitable[str]]:
    """Headless solicit: deliver `message` once, then signal a clean detach and block."""
    delivered = False

    async def _solicit(_prompt: str) -> str:
        nonlocal delivered
        if message is not None and not delivered:
            delivered = True
            return message
        # Trip the escalation event so run_terminal_client tears the client down
        # (run survives, the daemon hibernates at the gate); then block — never
        # returning so no stray reply is sent as an input frame.
        coord.escalation_event.set()
        await anyio.sleep_forever()
        raise AssertionError("unreachable")  # anyio 4.8: sleep_forever is -> None, not NoReturn

    return _solicit


def make_interactive_solicit(
    change: Change,
    *,
    status: Any,
    driver: Any,
    coord: InterruptCoordinator,
) -> Callable[[str], Awaitable[str]]:
    """Return a solicit callback that reads one slash-aware line from the tty as the gate reply."""
    # Lazy import: imp.cli.main imports this module, so a
    # module-level import of these helpers would be circular.
    from imp.cli.main import _editing_mode, _term_width

    prompt = Prompt(
        skills_dir=change.workflow_dir / "skills",
        editing_mode=_editing_mode(change.control_root),
        color=should_use_color(sys.stdout))

    async def _solicit(_prompt: str) -> str:
        # The gate's prompt (the interrupt-steer hint, or the daemon's resume
        # prompt) surfaces as the field placeholder; an empty prompt — the bare
        # turn-end yield — falls back to the default "type your reply".
        return await ask_line_with_slash(
            prompt, status, change,
            color=should_use_color(sys.stdout), width=_term_width(),
            coord=coord, driver=driver, hint=_prompt or None)

    return _solicit
