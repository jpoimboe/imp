"""imp CLI entrypoint.

Plan 1: minimal commands (new, resume, log, status). No streaming, no signals,
no spinner. Plan 3 adds the polished UX layer.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import anyio

from imp.cli.chat import ChatRenderer
from imp.cli.init import run_init
from imp.cli.prompt import Prompt
from imp.cli.signals import install_process_group, pause_and_exit
from imp.engine.change import Change
from imp.engine.errors import LoadError, LoadErrors
from imp.engine.executor import RunResult, run_change
from imp.engine.project_root import find_project_root
from imp.engine.state import LockHeld, ProjectLock, State


def _project_root() -> Path:
    return find_project_root()


def _write_change_lock(change: Change) -> None:
    """Informational per-Change PID file (spec §5.5). Removed on exit. The
    ProjectLock already enforces single-runner; this just gives `imp status`
    something to display."""
    (change.change_dir / "lock").write_text(str(os.getpid()))


def _clear_change_lock(change: Change) -> None:
    with contextlib.suppress(FileNotFoundError):
        (change.change_dir / "lock").unlink()


def _cmd_init(args: argparse.Namespace) -> int:
    project = _project_root()
    try:
        run_init(project, input_fn=input, output_fn=print)
    except FileExistsError as e:
        print(f"imp: {e}", file=sys.stderr)
        return 2
    return 0


async def _async_cmd_new(args: argparse.Namespace) -> int:
    install_process_group()
    project = _project_root()
    lock = ProjectLock(project / ".imp" / "lock")
    from imp.cli.signals import InterruptCoordinator

    try:
        with lock.acquire():
            try:
                change = Change.create(project, title=args.title)
            except (LoadError, LoadErrors) as e:
                print(f"imp: {e}", file=sys.stderr)
                return 2
            _write_change_lock(change)
            try:
                print(f"Created Change {change.id}: {change.title!r}")
                prompt = Prompt()
                renderer = ChatRenderer()
                coord = InterruptCoordinator()
                rc = 0
                try:
                    async with anyio.create_task_group() as tg:
                        tg.start_soon(coord.run_signal_receiver)
                        try:
                            result = await run_change(
                                change,
                                on_event=renderer.handle,
                                human_input_fn=lambda: prompt.ask_async("> "),
                                human_output_fn=renderer.line,
                                coordinator=coord,
                            )
                        except (LoadError, LoadErrors) as e:
                            print(f"imp: {e}", file=sys.stderr)
                            rc = 2
                        except EOFError:
                            pause_and_exit(change, reason="ctrl-d")
                            print(f"\nChange {change.id}: paused (ctrl-d)")
                            rc = 130
                        else:
                            if coord.should_pause:
                                pause_and_exit(change, reason="ctrl-c")
                                print(f"\nChange {change.id}: paused (ctrl-c)")
                                rc = 130
                            else:
                                print(f"Change {change.id}: {result.value}")
                                rc = 0 if result is RunResult.COMPLETED else 1
                        finally:
                            tg.cancel_scope.cancel()
                finally:
                    renderer.close()
                return rc
            finally:
                _clear_change_lock(change)
    except LockHeld as lock_err:
        print(
            f"imp: another Change is running (lock held by pid {lock_err.held_by_pid})",
            file=sys.stderr,
        )
        return 2


def _cmd_new(args: argparse.Namespace) -> int:
    return anyio.run(_async_cmd_new, args)


def _replay_current_step(change: Change, state: State, renderer: ChatRenderer) -> None:
    if state.step is None:
        return
    events = list(change.event_log().events_for_current_step(state.step))
    if not events:
        return
    print("── replaying current step context ──")
    for e in events:
        renderer.handle(e)
    print("── end replay ──")


async def _async_cmd_resume(args: argparse.Namespace) -> int:
    install_process_group()
    project = _project_root()
    lock = ProjectLock(project / ".imp" / "lock")
    from imp.cli.signals import InterruptCoordinator

    try:
        with lock.acquire():
            try:
                change = Change.load(project, args.change_id)
            except (LoadError, LoadErrors) as err:
                print(f"imp: {err}", file=sys.stderr)
                return 2
            state = State.load(change.state_dir)
            if state.status not in ("paused", "errored"):
                print(
                    f"imp: Change {change.id} status is {state.status!r}; "
                    "nothing to resume",
                    file=sys.stderr,
                )
                return 2
            _write_change_lock(change)
            try:
                print(f"Resuming Change {change.id}: {change.title!r}")
                print(f"Phase: {state.phase}, step: {state.step}")
                prompt = Prompt()
                renderer = ChatRenderer()
                coord = InterruptCoordinator()
                rc = 0
                try:
                    renderer.handle({"type": "workflow_resumed", "payload": {}})
                    change.event_log().append("workflow_resumed", {})
                    _replay_current_step(change, state, renderer)
                    state.status = "running"
                    state.save(change.state_dir)
                    async with anyio.create_task_group() as tg:
                        tg.start_soon(coord.run_signal_receiver)
                        try:
                            result = await run_change(
                                change,
                                on_event=renderer.handle,
                                human_input_fn=lambda: prompt.ask_async("> "),
                                human_output_fn=renderer.line,
                                coordinator=coord,
                            )
                        except (LoadError, LoadErrors) as e:
                            print(f"imp: {e}", file=sys.stderr)
                            rc = 2
                        except EOFError:
                            pause_and_exit(change, reason="ctrl-d")
                            print(f"\nChange {change.id}: paused (ctrl-d)")
                            rc = 130
                        else:
                            if coord.should_pause:
                                pause_and_exit(change, reason="ctrl-c")
                                print(f"\nChange {change.id}: paused (ctrl-c)")
                                rc = 130
                            else:
                                print(f"Change {change.id}: {result.value}")
                                rc = 0 if result is RunResult.COMPLETED else 1
                        finally:
                            tg.cancel_scope.cancel()
                finally:
                    renderer.close()
                return rc
            finally:
                _clear_change_lock(change)
    except LockHeld as lock_err:
        print(
            f"imp: another Change is running (lock held by pid {lock_err.held_by_pid})",
            file=sys.stderr,
        )
        return 2


def _cmd_resume(args: argparse.Namespace) -> int:
    return anyio.run(_async_cmd_resume, args)


def _cmd_log(args: argparse.Namespace) -> int:
    project = _project_root()
    change_id = args.change_id
    if change_id is None:
        changes_root = project / ".imp" / "changes"
        if not changes_root.is_dir():
            print("imp: no Changes yet", file=sys.stderr)
            return 2
        ids = sorted(
            int(d.name) for d in changes_root.iterdir()
            if d.is_dir() and d.name.isdigit()
        )
        if not ids:
            print("imp: no Changes yet", file=sys.stderr)
            return 2
        change_id = ids[-1]
    try:
        change = Change.load(project, change_id)
    except LoadError as err:
        print(f"imp: {err}", file=sys.stderr)
        return 2
    for e in change.event_log().read():
        ts = e["timestamp"][:19].replace("T", " ")
        if args.full:
            print(f"{ts}  {e['type']:<18}  {json.dumps(e['payload'])}")
        else:
            summary = _summarize_event(e)
            print(f"{ts}  {e['type']:<18}  {summary}")
    return 0


def _summarize_event(e: dict[str, Any]) -> str:
    t = e["type"]
    p = e["payload"]
    if t == "step_started":
        return f"{p.get('step', '')} [model={p.get('model', '')}]"
    if t == "step_completed":
        return f"{p.get('step', '')}  → {p.get('action', '')}"
    if t == "agent_output":
        return " ".join(f"{k}={v}" for k, v in p.items())
    if t == "phase_started":
        return str(p.get("phase", ""))
    if t == "phase_completed":
        return f"{p.get('phase', '')}  → {p.get('action', '')}"
    if t == "agent_message":
        text = str(p.get("text", ""))
        return text if len(text) < 60 else text[:57] + "..."
    return json.dumps(p)[:80]


def _last_event_timestamp(events_path: Path) -> str:
    """Return the timestamp of the last event in events.jsonl as local-time
    ``YYYY-MM-DD HH:MM:SS``, or ``'-'`` if the file is missing, empty, or
    has a malformed trailing line.
    """
    try:
        text = events_path.read_text()
    except FileNotFoundError:
        return "-"
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return "-"
    try:
        record = json.loads(lines[-1])
        ts = datetime.fromisoformat(record["timestamp"]).astimezone()
        return ts.strftime("%Y-%m-%d %H:%M:%S")
    except (json.JSONDecodeError, KeyError, ValueError):
        return "-"


def _cmd_status(args: argparse.Namespace) -> int:
    project = _project_root()
    changes_root = project / ".imp" / "changes"
    if not changes_root.is_dir():
        print("No Changes yet.")
        return 0
    valid_dirs = [
        d for d in changes_root.iterdir()
        if d.is_dir() and d.name.isdigit()
    ]
    rows = []
    for d in sorted(valid_dirs, key=lambda p: int(p.name)):
        meta = json.loads((d / "change.json").read_text())
        state = State.load(d / "state")
        last_activity = _last_event_timestamp(d / "events.jsonl")
        rows.append(
            (meta["id"], state.status, state.phase or "-",
             state.step or "-", last_activity, meta["title"])
        )
    print(f"{'ID':<4} {'STATUS':<10} {'PHASE':<12} {'STEP':<20} {'LAST_ACTIVITY':<20} TITLE")
    for row in rows:
        print(f"{row[0]:<4} {row[1]:<10} {row[2]:<12} {row[3]:<20} {row[4]:<20} {row[5]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(prog="imp")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init")
    p_init.set_defaults(func=_cmd_init)

    p_new = sub.add_parser("new")
    p_new.add_argument("title", nargs="?")
    p_new.set_defaults(func=_cmd_new)

    p_resume = sub.add_parser("resume")
    p_resume.add_argument("change_id", type=int)
    p_resume.set_defaults(func=_cmd_resume)

    p_log = sub.add_parser("log")
    p_log.add_argument("change_id", type=int, nargs="?")
    p_log.add_argument("--full", action="store_true")
    p_log.set_defaults(func=_cmd_log)

    p_status = sub.add_parser("status")
    p_status.set_defaults(func=_cmd_status)

    ns = parser.parse_args(args)
    rc: int = ns.func(ns)
    return rc
