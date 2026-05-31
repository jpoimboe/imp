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
import shutil
import subprocess
import sys
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

import anyio

from imp.cli.init import run_init
from imp.cli.live_driver import LiveDriver
from imp.cli.live_status import LiveStatus
from imp.cli.palette import should_use_color
from imp.cli.prompt import Prompt
from imp.cli.signals import install_process_group, pause_and_exit
from imp.engine import worktree
from imp.engine.change import Change
from imp.engine.errors import LoadError, LoadErrors
from imp.engine.executor import RunResult, run_change
from imp.engine.naming import random_title
from imp.engine.project_root import find_project_root
from imp.engine.slug import InvalidSlug, slugify
from imp.engine.state import LockHeld, ProjectLock, State


def _project_root() -> Path:
    return find_project_root()


def _control_root() -> Path:
    return worktree.find_control_root(Path.cwd())


def _select_slug(control_root: Path, *, title: str | None) -> tuple[str, str]:
    """Return (slug, title). Title-derived collision is a clean error; the
    random-phrase default retries on the rare collision (spec §3.1)."""
    if title is not None:
        try:
            slug = slugify(title)
        except InvalidSlug:
            print(f"imp: title {title!r} has no slug-able characters; pass a real title",
                  file=sys.stderr)
            raise SystemExit(2) from None
        if worktree.branch_exists(control_root, f"imp/{slug}"):
            print(f"imp: a Change named {slug!r} already exists; pick a different "
                  "title", file=sys.stderr)
            raise SystemExit(2)
        return slug, title
    for _ in range(20):
        t = random_title()
        slug = slugify(t)
        if not worktree.branch_exists(control_root, f"imp/{slug}"):
            return slug, t
    raise SystemExit("imp: could not allocate a unique random name; retry")


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
    from imp.cli.signals import InterruptCoordinator

    try:
        control = _control_root()
        worktree.check_preconditions(control)
        worktree.ensure_imp_ignored(control)
        base = worktree.current_branch(control)
    except worktree.PreconditionError as e:
        print(f"imp: {e}", file=sys.stderr)
        return 2

    change = None
    for _ in range(20):
        try:
            slug, title = _select_slug(control, title=args.title)
        except SystemExit:
            return 2                                   # title collision/invalid (printed)
        try:
            change = Change.create(control, title=title, slug=slug, base_branch=base)
            break
        except (LoadError, LoadErrors) as e:
            print(f"imp: {e}", file=sys.stderr)
            return 2
        except worktree.BranchExists:
            if args.title is not None:                 # title-derived: clean error
                print(f"imp: a Change named {slug!r} already exists", file=sys.stderr)
                return 2
            continue                                   # random: re-draw and retry
    if change is None:
        print("imp: could not allocate a unique Change name; retry", file=sys.stderr)
        return 2

    try:
        with ProjectLock(change.change_dir / "lock").acquire():  # per-Change single-runner
            print(f"Created Change {change.slug}: {change.title!r}")
            prompt = Prompt()
            status = LiveStatus(sys.stdout)
            driver = LiveDriver(stream=sys.stdout,
                                color=should_use_color(sys.stdout),
                                width=_term_width(), status=status)
            coord = InterruptCoordinator()
            rc = 0

            async def _ask_line() -> str:
                with status.suspend():
                    return await prompt.ask_async("> ")

            async with anyio.create_task_group() as tg:
                tg.start_soon(coord.run_signal_receiver)
                tg.start_soon(status.tick)
                try:
                    result = await run_change(
                        change,
                        on_event=driver.on_event,
                        human_input_fn=_ask_line,
                        human_output_fn=driver.print_chrome,
                        user_chat_input_fn=lambda _q: _ask_line(),
                        coordinator=coord,
                    )
                except (LoadError, LoadErrors) as e:
                    driver.finish()
                    print(f"imp: {e}", file=sys.stderr)
                    rc = 2
                except EOFError:
                    driver.finish()
                    pause_and_exit(change, reason="ctrl-d")
                    print(f"\nChange {change.slug}: paused (ctrl-d)")
                    rc = 130
                else:
                    driver.finish()
                    if coord.should_pause:
                        pause_and_exit(change, reason="ctrl-c")
                        print(f"\nChange {change.slug}: paused (ctrl-c)")
                        rc = 130
                    else:
                        print(f"Change {change.slug}: {result.value}")
                        rc = 0 if result is RunResult.COMPLETED else 1
                finally:
                    status.reset()
                    tg.cancel_scope.cancel()
            return rc
    except LockHeld as e:
        print(f"imp: Change {change.slug} is already running (pid {e.held_by_pid})",
              file=sys.stderr)
        return 2


def _cmd_new(args: argparse.Namespace) -> int:
    return anyio.run(_async_cmd_new, args)


async def _async_cmd_resume(args: argparse.Namespace) -> int:
    install_process_group()
    from imp.cli.signals import InterruptCoordinator

    control = _control_root()
    try:
        change = Change.load(control, args.slug)
    except (LoadError, LoadErrors) as err:
        print(f"imp: {err}", file=sys.stderr)
        return 2
    state = State.load(change.state_dir)
    if state.status not in ("paused", "errored", "merge_blocked"):
        print(
            f"imp: Change {change.slug} status is {state.status!r}; "
            "nothing to resume",
            file=sys.stderr,
        )
        return 2
    try:
        with ProjectLock(change.change_dir / "lock").acquire():  # per-Change single-runner
            print(f"Resuming Change {change.slug}: {change.title!r}")
            prompt = Prompt()
            status = LiveStatus(sys.stdout)
            driver = LiveDriver(stream=sys.stdout,
                                color=should_use_color(sys.stdout),
                                width=_term_width(), status=status)
            coord = InterruptCoordinator()
            rc = 0
            # Append the resume marker exactly once (for the persisted log /
            # imp log). NO replay: the engine picks up from the current step
            # cursor, re-emitting step_started + the step body fresh
            # (design "Resume — no replay").
            change.event_log().append(
                agent_id=None, type="workflow_resumed", payload={})
            state.status = "running"
            state.save(change.state_dir)

            async def _ask_line() -> str:
                with status.suspend():
                    return await prompt.ask_async("> ")

            async with anyio.create_task_group() as tg:
                tg.start_soon(coord.run_signal_receiver)
                tg.start_soon(status.tick)
                try:
                    result = await run_change(
                        change,
                        on_event=driver.on_event,
                        human_input_fn=_ask_line,
                        human_output_fn=driver.print_chrome,
                        user_chat_input_fn=lambda _q: _ask_line(),
                        coordinator=coord,
                    )
                except (LoadError, LoadErrors) as e:
                    driver.finish()
                    print(f"imp: {e}", file=sys.stderr)
                    rc = 2
                except EOFError:
                    driver.finish()
                    pause_and_exit(change, reason="ctrl-d")
                    print(f"\nChange {change.slug}: paused (ctrl-d)")
                    rc = 130
                else:
                    driver.finish()
                    if coord.should_pause:
                        pause_and_exit(change, reason="ctrl-c")
                        print(f"\nChange {change.slug}: paused (ctrl-c)")
                        rc = 130
                    else:
                        print(f"Change {change.slug}: {result.value}")
                        rc = 0 if result is RunResult.COMPLETED else 1
                finally:
                    status.reset()
                    tg.cancel_scope.cancel()
            return rc
    except LockHeld as e:
        print(f"imp: Change {change.slug} is already running (pid {e.held_by_pid})",
              file=sys.stderr)
        return 2


def _cmd_resume(args: argparse.Namespace) -> int:
    return anyio.run(_async_cmd_resume, args)


def _term_width() -> int:
    # shutil.get_terminal_size honors $COLUMNS and substitutes the fallback when
    # the OS reports columns <= 0 (e.g. a winsize-0 PTY), unlike
    # os.get_terminal_size which returns a literal 0 and floors wrapping to 8
    # chars (issue 2). Defense-in-depth with the dogfood PTY winsize fix.
    return shutil.get_terminal_size((80, 24)).columns


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


def _list_change_worktrees(control: Path) -> list[str]:
    """Slugs of in-flight Change worktrees under .imp/worktrees/ (excludes the
    transient .merge-* scratch dirs)."""
    root = control / ".imp" / "worktrees"
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir()
                  if p.is_dir() and not p.name.startswith(".")
                  and (p / ".imp" / "change" / "change.json").exists())


def _cmd_log(args: argparse.Namespace) -> int:
    from imp.cli import narrative
    from imp.cli.render_text import render_tree

    control = _control_root()
    slug = args.slug
    if slug is None:
        wts = _list_change_worktrees(control)
        if not wts:
            print("No in-flight Changes. Completed Changes: git log refs/imp/journal",
                  file=sys.stderr)
            return 2
        slug = max(wts, key=lambda s: (control / ".imp" / "worktrees" / s
                                       / ".imp" / "change" / "events.jsonl").stat().st_mtime)
    try:
        change = Change.load(control, slug)
    except LoadError as err:
        print(f"imp: {err}", file=sys.stderr)
        return 2

    events = list(change.event_log().read())
    tree = narrative.build_tree(events)

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
            (meta["id"], state.status, state.step or "-",
             last_activity, meta["title"])
        )
    print(f"{'ID':<4} {'STATUS':<10} {'STEP':<20} {'LAST_ACTIVITY':<20} TITLE")
    for row in rows:
        print(f"{row[0]:<4} {row[1]:<10} {row[2]:<20} {row[3]:<20} {row[4]}")
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
    p_resume.add_argument("slug")
    p_resume.set_defaults(func=_cmd_resume)

    p_log = sub.add_parser(
        "log",
        help="Show the narrative for a Change.",
        description=(
            "Show the narrative for a Change as plain text. --full starts every "
            "fold expanded. Set NO_COLOR=1 to disable colors. "
            "Tip: imp log <slug> --full | grep ... for line-oriented scripting."
        ),
    )
    p_log.add_argument("slug", nargs="?")
    p_log.add_argument(
        "--full", action="store_true",
        help="Render every event (no tool-burst collapse).",
    )
    p_log.add_argument(
        "--no-pager", action="store_true",
        help="Disable the pager even when stdout is a TTY.",
    )
    p_log.set_defaults(func=_cmd_log)

    p_status = sub.add_parser("status")
    p_status.set_defaults(func=_cmd_status)

    ns = parser.parse_args(args)
    rc: int = ns.func(ns)
    return rc
