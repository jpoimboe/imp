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
from typing import Any

import anyio

from imp.cli.init import run_init
from imp.cli.live_driver import LiveDriver
from imp.cli.live_status import LiveStatus
from imp.cli.palette import should_use_color
from imp.cli.prompt import Prompt
from imp.cli.signals import install_process_group, pause_and_exit
from imp.cli.slash import ask_line_with_slash
from imp.engine import journal, worktree
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
            prompt = Prompt(skills_dir=change.workflow_dir / "skills")
            status = LiveStatus(sys.stdout)
            driver = LiveDriver(stream=sys.stdout,
                                color=should_use_color(sys.stdout),
                                width=_term_width(), status=status)
            coord = InterruptCoordinator()
            rc = 0

            async def _ask_line() -> str:
                return await ask_line_with_slash(prompt, status, change)

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
                        if result is RunResult.COMPLETED:
                            # Print merge headline when the worktree was removed by
                            # merge (worktree gone = merge gate was exercised).
                            wt_path = change.control_root / ".imp" / "worktrees" / change.slug
                            if not wt_path.exists():
                                print(f"Merged {change.slug} into {change.base_branch}.")
                            print(f"Change {change.slug}: {result.value}")
                            rc = 0
                        else:
                            # PAUSED — check for merge_blocked and print remediation.
                            final_state = State.load(change.state_dir)
                            if final_state.status == "merge_blocked":
                                # Read the reason from the last merge_blocked event.
                                reason = None
                                for ev in change.event_log().read():
                                    if ev.get("type") == "merge_blocked":
                                        reason = (ev.get("payload") or {}).get("reason")
                                base = change.base_branch
                                slug = change.slug
                                if reason == "worktree_dirty":
                                    print(f"Uncommitted work in the worktree; commit it, "
                                          f"then `imp resume {slug}`.")
                                elif reason == "base_dirty":
                                    print(f"Uncommitted changes on `{base}`; commit or stash "
                                          f"them, then `imp resume {slug}`.")
                                elif reason == "conflict":
                                    print(f"`imp/{slug}` doesn't auto-merge into `{base}`. "
                                          f"imp can't resolve conflicts in this version — "
                                          f"merge `imp/{slug}` into `{base}` manually, then "
                                          f"the Change is done (its worktree at "
                                          f"`.imp/worktrees/{slug}` can be removed).")
                                else:
                                    print(f"Change {change.slug}: merge_blocked. "
                                          f"Resolve, then `imp resume {slug}`.")
                            print(f"Change {change.slug}: {result.value}")
                            rc = 1
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
            prompt = Prompt(skills_dir=change.workflow_dir / "skills")
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
                return await ask_line_with_slash(prompt, status, change)

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
                        if result is RunResult.COMPLETED:
                            # Print merge headline when the worktree was removed by
                            # merge (worktree gone = merge gate was exercised).
                            wt_path = change.control_root / ".imp" / "worktrees" / change.slug
                            if not wt_path.exists():
                                print(f"Merged {change.slug} into {change.base_branch}.")
                            print(f"Change {change.slug}: {result.value}")
                            rc = 0
                        else:
                            # PAUSED — check for merge_blocked and print remediation.
                            final_state = State.load(change.state_dir)
                            if final_state.status == "merge_blocked":
                                # Read the reason from the last merge_blocked event.
                                reason = None
                                for ev in change.event_log().read():
                                    if ev.get("type") == "merge_blocked":
                                        reason = (ev.get("payload") or {}).get("reason")
                                base = change.base_branch
                                slug = change.slug
                                if reason == "worktree_dirty":
                                    print(f"Uncommitted work in the worktree; commit it, "
                                          f"then `imp resume {slug}`.")
                                elif reason == "base_dirty":
                                    print(f"Uncommitted changes on `{base}`; commit or stash "
                                          f"them, then `imp resume {slug}`.")
                                elif reason == "conflict":
                                    print(f"`imp/{slug}` doesn't auto-merge into `{base}`. "
                                          f"imp can't resolve conflicts in this version — "
                                          f"merge `imp/{slug}` into `{base}` manually, then "
                                          f"the Change is done (its worktree at "
                                          f"`.imp/worktrees/{slug}` can be removed).")
                                else:
                                    print(f"Change {change.slug}: merge_blocked. "
                                          f"Resolve, then `imp resume {slug}`.")
                            print(f"Change {change.slug}: {result.value}")
                            rc = 1
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


def _parse_jsonl(raw: bytes) -> list[dict[str, Any]]:
    """Parse journaled events.jsonl bytes into event dicts, mirroring
    JsonlEventLog.read: malformed lines become {"_malformed": True, ...}."""
    out: list[dict[str, Any]] = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            out.append({"_malformed": True, "raw": line})
    return out


def _load_events_for_slug(control: Path, slug: str) -> list[dict[str, Any]] | None:
    """Worktree first, journal second. Returns None if the slug is in neither.

    Worktree-first is necessary, not merely preferred: a slug lives in exactly
    one place (journal_commit runs once, concurrent with worktree removal), so
    the only overlap is a crash mid-sequence where worktree-first harmlessly
    returns identical content."""
    try:
        change = Change.load(control, slug)
    except LoadError:
        change = None
    if change is not None:
        return list(change.event_log().read())
    raw = journal.journal_show(control, slug, "events.jsonl")
    if raw is None:
        return None
    return _parse_jsonl(raw)


def _resolve_default_slug(control: Path) -> str | None:
    """No-slug default: most-recent in-flight worktree, else most-recent
    journaled slug (by last-event timestamp), else None."""
    wts = _list_change_worktrees(control)
    if wts:
        return max(wts, key=lambda s: (control / ".imp" / "worktrees" / s
                                       / ".imp" / "change" / "events.jsonl").stat().st_mtime)
    journaled = journal.journal_slugs(control)
    if not journaled:
        return None
    return max(journaled, key=lambda s: _journal_last_activity(control, s))


def _cmd_log(args: argparse.Namespace) -> int:
    from imp.cli import narrative
    from imp.cli.render_text import render_tree

    control = _control_root()
    slug = args.slug if args.slug is not None else _resolve_default_slug(control)
    if slug is None:
        print("imp: no Changes found (no in-flight or completed Changes)",
              file=sys.stderr)
        return 2

    events = _load_events_for_slug(control, slug)
    if events is None:
        print(f"imp: no such Change {slug!r}", file=sys.stderr)
        return 2

    tree = narrative.build_tree(events)
    width = _term_width()
    color = should_use_color(sys.stdout)
    lines = render_tree(tree, full=args.full, color=color, width=width)
    _page_or_print(lines, no_pager=args.no_pager)
    return 0


def _format_last_timestamp(text: str) -> str:
    """Local-time YYYY-MM-DD HH:MM:SS of the last event line in `text`, or '-'
    when the text is empty / has a malformed trailing line."""
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return "-"
    try:
        record = json.loads(lines[-1])
        ts = datetime.fromisoformat(record["timestamp"]).astimezone()
        return ts.strftime("%Y-%m-%d %H:%M:%S")
    except (json.JSONDecodeError, KeyError, ValueError):
        return "-"


def _last_event_timestamp(events_path: Path) -> str:
    try:
        text = events_path.read_text()
    except FileNotFoundError:
        return "-"
    return _format_last_timestamp(text)


def _journal_last_activity(control: Path, slug: str) -> str:
    raw = journal.journal_show(control, slug, "events.jsonl")
    if raw is None:
        return "-"
    return _format_last_timestamp(raw.decode("utf-8", errors="replace"))


# Spec §10: in-flight statuses; completed Changes live in the journal.
_INFLIGHT = ("running", "paused", "merge_blocked")


def _status_row(slug: str, state_json: bytes, change_json: bytes,
                last_activity: str) -> tuple[str, str, str, str, str]:
    """Pure (slug, status, step, last_activity, title) extractor, fed by either
    worktree file bytes or journal_show bytes. The journaled state.json and
    change.json are schema-identical to the in-flight ones (both written by
    State.save / Change.create), so the two sections can't silently drift."""
    st = json.loads(state_json)
    meta = json.loads(change_json)
    return (slug, st["status"], st.get("step") or "-", last_activity, meta["title"])


def _status_rows(control: Path) -> list[tuple[str, str, str, str, str]]:
    rows: list[tuple[str, str, str, str, str]] = []
    for slug in _list_change_worktrees(control):
        cdir = control / ".imp" / "worktrees" / slug / ".imp" / "change"
        state_json = (cdir / "state" / "state.json").read_bytes()
        change_json = (cdir / "change.json").read_bytes()
        last = _last_event_timestamp(cdir / "events.jsonl")
        row = _status_row(slug, state_json, change_json, last)
        if row[1] not in _INFLIGHT:
            continue
        rows.append(row)
    return rows


def _completed_rows(control: Path) -> list[tuple[str, str, str, str, str]]:
    """Journal-backed completed Changes: every journaled slug NOT present as a
    live worktree (in-flight takes precedence on the crash-window overlap)."""
    live = set(_list_change_worktrees(control))
    rows: list[tuple[str, str, str, str, str]] = []
    for slug in journal.journal_slugs(control):
        if slug in live:
            continue
        state_json = journal.journal_show(control, slug, "state.json")
        change_json = journal.journal_show(control, slug, "change.json")
        if state_json is None or change_json is None:
            continue
        last = _journal_last_activity(control, slug)
        row = _status_row(slug, state_json, change_json, last)
        if row[1] in _INFLIGHT:
            continue  # belt-and-suspenders; journaled rows are completed
        rows.append(row)
    return rows


_STATUS_HEADER = (
    f"{'SLUG':<24} {'STATUS':<12} {'STEP':<18} {'LAST_ACTIVITY':<20} TITLE"
)


def _print_status_section(title: str, rows: list[tuple[str, str, str, str, str]],
                          empty_msg: str) -> None:
    print(title)
    if rows:
        print(_STATUS_HEADER)
        for r in rows:
            print(f"{r[0]:<24} {r[1]:<12} {r[2]:<18} {r[3]:<20} {r[4]}")
    else:
        print(empty_msg)


def _cmd_status(args: argparse.Namespace) -> int:
    try:
        control = _control_root()
    except worktree.PreconditionError:
        _print_status_section("In-flight Changes", [], "No in-flight Changes.")
        print()
        _print_status_section("Completed Changes", [], "No completed Changes.")
        return 0
    _print_status_section("In-flight Changes", _status_rows(control),
                          "No in-flight Changes.")
    print()
    _print_status_section("Completed Changes", _completed_rows(control),
                          "No completed Changes.")
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
