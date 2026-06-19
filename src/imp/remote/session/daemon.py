"""Per-change session-daemon: bind run_change behind a SessionServer."""
from __future__ import annotations

import argparse
import functools
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import anyio

from imp.engine.abandon import tear_down_change
from imp.engine.change import Change
from imp.engine.errors import LoadError, LoadErrors
from imp.engine.executor import RunResult, run_change
from imp.engine.merge_queue import ensure_head_driven
from imp.engine.recovery import GateBypassRequired, RewindError
from imp.engine.state import EventLog, LockHeld, ProjectLock, State
from imp.remote.session.discover import session_socket_path
from imp.remote.session.server import SessionServer

RunFn = Callable[..., Awaitable[Any]]

_RESUME_PROMPT = "paused — type a message to redirect, or detach to leave it paused"


def _is_interrupt_pause(result: Any, coordinator: Any) -> bool:
    return result is RunResult.PAUSED and coordinator.should_pause


def _trailing_is_workflow_resumed(log: EventLog) -> bool:
    entries = log.tail(2)
    if entries and entries[-1].get("type") == "step_rewound":
        entries = entries[:-1]
    return bool(entries) and entries[-1].get("type") == "workflow_resumed"


def _append_workflow_resumed(log: EventLog) -> None:
    if not _trailing_is_workflow_resumed(log):
        log.append(agent_id=None, type="workflow_resumed", payload={})


def _last_pause_was_awaiting_turn(change: Change) -> bool:
    for event in reversed(list(change.event_log().read())):
        etype = event.get("type")
        if etype == "workflow_paused":
            return (event.get("payload") or {}).get("reason") == "awaiting_turn"
        if etype == "workflow_resumed":
            return False
    return False


async def serve_change_session(
    change: Change,
    *,
    run_fn: RunFn = run_change,
    hibernate: bool = True,
    tear_down: Callable[[Change], None] = tear_down_change,
    seed_opening: str | None = None,
    resume: bool = False,
    from_target: str | None = None,
) -> Any:
    # Validate from_target before opening the socket: a bad target raises
    # RewindError cleanly here, leaving no orphaned journal entry and no
    # ExceptionGroup wrapping from the server's task group.
    _rewind_plan: Any = None
    _rewind_workflow: Any = None
    if resume and from_target is not None:
        from imp.engine.loader import load_workflow
        from imp.engine.recovery import plan_rewind
        from imp.providers.effective import load_effective_config

        cfg = load_effective_config(
            project_root=change.project_root,
            control_root=change.control_root).config
        _rewind_workflow = load_workflow(
            change.workflow_dir, configured_tiers=cfg.tier_names(),
            project_keys=frozenset(cfg.project_facts))
        # Loaded before the lock: ProjectLock prevents concurrent mutation of
        # state_dir, so _pre_state.step is stable through _bound's execution.
        _rewind_plan = plan_rewind(
            _rewind_workflow, State.load(change.state_dir), from_target)

    # Shared coordinator: socket `interrupt` control frame → run_change's pause check.
    from imp.cli.signals import InterruptCoordinator

    coordinator = InterruptCoordinator()
    socket_path = str(session_socket_path(change.control_root, change.id))
    attach_log = change.event_log()

    def _on_first_attach() -> None:
        attach_log.append(agent_id=None, type="cli_attached", payload={})

    server = SessionServer(
        socket_path,
        coordinator=coordinator,
        lock=None,
        hibernate=hibernate,
        events_path=change.events_path,
        on_first_attach=_on_first_attach,
    )

    async def _bound(*, on_event: Any, user_chat_input_fn: Any) -> Any:
        from imp.agents.tools.wait_for_user import AwaitingUserInput

        if resume:
            log = change.event_log()
            state = State.load(change.state_dir)
            _append_workflow_resumed(log)
            # P1 crash recovery: a crashed run (status still "running" on disk)
            # never set resume_pending, so the executor would re-enter the current
            # step fresh and re-burn its work. Keep and replay it. Skipped on a
            # --from rewind, which manages resume_pending through apply_rewind.
            if from_target is None and state.status == "running":
                state.resume_pending = state.step
            state.status = "running"
            if _rewind_plan is not None:
                from imp.engine.recovery import apply_rewind
                # step_rewound follows workflow_resumed in the append-only log.
                apply_rewind(_rewind_plan, _rewind_workflow, state, log)
            state.save(change.state_dir)
        first_kwargs = (
            {"seed_opening": seed_opening} if seed_opening is not None else {})
        result = await run_fn(
            change,
            on_event=on_event,
            user_chat_input_fn=user_chat_input_fn,
            coordinator=coordinator,
            wait_for_turn_fn=server.wait_for_turn,
            **first_kwargs,
        )
        while _is_interrupt_pause(result, coordinator):
            prompt_event = {"type": "awaiting_user_input",
                            "payload": {"prompt": _RESUME_PROMPT}}
            server.set_pending_prompt(prompt_event)
            on_event(prompt_event)
            try:
                reply = await user_chat_input_fn("")
            except AwaitingUserInput:
                return result
            finally:
                server.set_pending_prompt(None)
            coordinator.reset()
            if reply.strip():
                coordinator.enqueue_interject(reply)
            _append_workflow_resumed(change.event_log())
            result = await run_fn(
                change,
                on_event=on_event,
                user_chat_input_fn=user_chat_input_fn,
                coordinator=coordinator,
                wait_for_turn_fn=server.wait_for_turn,
            )
        return result

    try:
        with ProjectLock(change.change_dir / "lock").acquire():
            await server.open()
            result = await server.serve(_bound)
    except LockHeld:
        # A wake-spawn raced a resident change: exit harmlessly without binding socket.
        return None
    if server.abandoned:
        # cwd is control_root, not the worktree, so worktree removal is safe
        await anyio.to_thread.run_sync(tear_down, change)
    elif result is RunResult.PAUSED and _last_pause_was_awaiting_turn(change):
        await anyio.to_thread.run_sync(
            ensure_head_driven, change.control_root, change.base_branch)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="imp-session-daemon")
    parser.add_argument("--control-root", required=True)
    parser.add_argument("--change-id", required=True)
    parser.add_argument("--seed-opening", default=None)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--from", dest="from_target", default=None)
    ns = parser.parse_args(argv)
    try:
        change = Change.load(Path(ns.control_root), ns.change_id)
    except LoadError as exc:
        print(f"imp: {exc}", file=sys.stderr)
        return 2
    kwargs: dict[str, Any] = {}
    if ns.seed_opening is not None:
        kwargs["seed_opening"] = ns.seed_opening
    if ns.resume:
        kwargs["resume"] = True
    if ns.from_target is not None:
        kwargs["from_target"] = ns.from_target
    try:
        if kwargs:
            result = anyio.run(functools.partial(serve_change_session, change, **kwargs))
        else:
            result = anyio.run(serve_change_session, change)
    except (LoadError, LoadErrors, RewindError, GateBypassRequired) as exc:
        print(f"imp: {exc}", file=sys.stderr)
        return 2
    return 1 if result is RunResult.PAUSED else 0


if __name__ == "__main__":
    raise SystemExit(main())
