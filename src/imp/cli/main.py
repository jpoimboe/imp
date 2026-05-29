"""imp CLI entrypoint.

Plan 1: minimal commands (new, resume, log, status). No streaming, no signals,
no spinner. Plan 3 adds the polished UX layer.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shlex
import subprocess
import sys
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

import anyio

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
    except KeyboardInterrupt:
        print("\nimp: init cancelled", file=sys.stderr)
        return 130
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
                _emit_line, _print_text = _stream_sinks()
                coord = InterruptCoordinator()
                rc = 0
                async with anyio.create_task_group() as tg:
                    tg.start_soon(coord.run_signal_receiver)
                    try:
                        result = await run_change(
                            change,
                            on_event=_emit_line,
                            human_input_fn=lambda: prompt.ask_async("> "),
                            human_output_fn=_print_text,
                            user_chat_input_fn=lambda _q: prompt.ask_async("> "),
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
                _emit_line, _print_text = _stream_sinks()   # shared helper (Task 16)

                coord = InterruptCoordinator()
                rc = 0
                # --no-tui resume keeps the lighter current-step replay (no
                # scroll-back; design §4.2). The workflow_resumed marker is
                # still appended exactly once, as today.
                change.event_log().append(
                    agent_id=None, type="workflow_resumed", payload={})
                if state.step is not None:
                    for e in change.event_log().events_for_current_step(state.step):
                        _emit_line(e)
                state.status = "running"
                state.save(change.state_dir)
                async with anyio.create_task_group() as tg:
                    tg.start_soon(coord.run_signal_receiver)
                    try:
                        result = await run_change(
                            change,
                            on_event=_emit_line,
                            human_input_fn=lambda: prompt.ask_async("> "),
                            human_output_fn=_print_text,
                            user_chat_input_fn=lambda _q: prompt.ask_async("> "),
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


def _stream_sinks():
    """Return (on_event, output) callbacks that stream the text view to stdout.
    Shared by the --no-tui paths of `imp new` and `imp resume`."""
    from imp.cli.palette import should_use_color
    from imp.cli.render_text import stream_event
    color = should_use_color(sys.stdout)

    def emit_line(event: dict) -> None:
        block = stream_event(event, color=color, width=_term_width())
        if block is not None:
            print(block)
            sys.stdout.flush()

    def output(text: str) -> None:
        print(text)
        sys.stdout.flush()

    return emit_line, output


def _use_tui(*, no_tui: bool, stream: Any = None) -> bool:
    """TUI is the default on a TTY; forced off by --no-tui or a non-TTY stdout
    (design §8)."""
    if no_tui:
        return False
    stream = stream if stream is not None else sys.stdout
    return bool(getattr(stream, "isatty", lambda: False)())


def _term_width() -> int:
    try:
        return os.get_terminal_size().columns
    except OSError:
        return 80


def _tui_available() -> bool:
    """True once the TUI entry point ``run_log_tui`` is importable (Task 27+).
    Until then — including while ``cli/tui.py`` is being built up incrementally
    across M4 but its entry point doesn't yet exist — a TTY degrades to the text
    view rather than raising ImportError. Checking the symbol (not just the
    module's existence) keeps the suite green during M4 construction. Only called
    when ``_use_tui`` is already True (a real TTY), so importing here is fine."""
    import importlib.util
    if importlib.util.find_spec("imp.cli.tui") is None:
        return False
    return hasattr(importlib.import_module("imp.cli.tui"), "run_log_tui")


def _page_or_print(lines: Iterable[str], *, no_pager: bool) -> None:
    """Write rendered ``lines`` to stdout, paging through ``$PAGER`` (default
    ``less -FRX``) when ``sys.stdout.isatty()`` is true, ``no_pager`` is
    False, and ``$PAGER`` is not set to an empty/whitespace string.

    Falls back to ``print()`` per line when paging doesn't apply or the
    pager binary cannot be started. Each input line should NOT have a
    trailing newline; the helper adds one itself.
    """
    if no_pager or not sys.stdout.isatty():
        for line in lines:
            print(line)
        return

    pager_env = os.environ.get("PAGER")
    if pager_env is not None:
        stripped = pager_env.strip()
        if not stripped:
            # PAGER="" or whitespace: explicit disable, like git.
            for line in lines:
                print(line)
            return
        try:
            cmd = shlex.split(stripped)
        except ValueError:
            # Malformed quoting in $PAGER (e.g. `less "unclosed`). Don't
            # crash `imp log` — degrade to inline output, same as a
            # missing pager binary.
            for line in lines:
                print(line)
            return
        if not cmd:
            # Defensive: post-strip non-empty value can't normally split
            # to empty, but if it does, fall through to inline output.
            for line in lines:
                print(line)
            return
    else:
        cmd = ["less", "-F", "-R", "-X"]

    # When the resolved pager is `less`, always pass an explicit `LESS`
    # value to the child env: use the user's existing value if set,
    # otherwise default to `FRX` so that `PAGER=less` behaves the same
    # as the unset-PAGER default (`less -FRX`).
    env: dict[str, str] | None = None
    if os.path.basename(cmd[0]) == "less":
        less_val = os.environ.get("LESS", "FRX")
        env = {**os.environ, "LESS": less_val}

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            text=True,
            env=env,
        )
    except (FileNotFoundError, OSError):
        # Pager binary missing or unexecutable → silent fallback.
        for line in lines:
            print(line)
        return

    assert proc.stdin is not None  # stdin=PIPE guarantees this for type-checkers
    try:
        with contextlib.suppress(BrokenPipeError):
            for line in lines:
                proc.stdin.write(line + "\n")
    finally:
        with contextlib.suppress(BrokenPipeError):
            proc.stdin.close()
        # Ctrl-C in the pager raises KeyboardInterrupt out of proc.wait().
        # Re-wait until the child actually exits so the user sees the pager
        # behave normally instead of a Python traceback.
        while True:
            try:
                proc.wait()
                break
            except KeyboardInterrupt:
                pass


def _cmd_log(args: argparse.Namespace) -> int:
    from imp.cli import narrative
    from imp.cli.render_text import render_tree

    project = _project_root()
    change_id = args.change_id
    if change_id is None:
        changes_root = project / ".imp" / "changes"
        if not changes_root.is_dir():
            print("imp: no Changes yet", file=sys.stderr)
            return 2
        ids = sorted(int(d.name) for d in changes_root.iterdir()
                     if d.is_dir() and d.name.isdigit())
        if not ids:
            print("imp: no Changes yet", file=sys.stderr)
            return 2
        change_id = ids[-1]
    try:
        change = Change.load(project, change_id)
    except LoadError as err:
        print(f"imp: {err}", file=sys.stderr)
        return 2

    events = list(change.event_log().read())
    tree = narrative.build_tree(events)

    if _use_tui(no_tui=args.no_tui) and _tui_available():
        from imp.cli.tui import run_log_tui            # built in M4
        return run_log_tui(tree, full=args.full, mouse=not args.no_mouse)

    from imp.cli.palette import should_use_color
    width = _term_width()
    color = should_use_color(sys.stdout)
    lines = render_tree(tree, full=args.full, color=color, width=width)
    _page_or_print(lines, no_pager=args.no_pager)
    return 0


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

    p_log = sub.add_parser(
        "log",
        help="Show the event log for a Change.",
        description=(
            "Show the event log for a Change. "
            "Auto-pages through $PAGER (default `less -FRX`) when stdout is a TTY. "
            "Set NO_COLOR=1 to disable colors."
        ),
    )
    p_log.add_argument("change_id", type=int, nargs="?")
    p_log.add_argument(
        "--full", action="store_true",
        help="Render every event (no tool-burst collapse).",
    )
    p_log.add_argument(
        "--no-pager", action="store_true",
        help="Disable the pager even when stdout is a TTY.",
    )
    p_log.add_argument("--no-tui", action="store_true",
                       help="Force the plain-text view even on a TTY.")
    p_log.add_argument("--no-mouse", action="store_true",
                       help="Disable TUI mouse capture.")
    p_log.set_defaults(func=_cmd_log)

    p_status = sub.add_parser("status")
    p_status.set_defaults(func=_cmd_status)

    ns = parser.parse_args(args)
    rc: int = ns.func(ns)
    return rc
