"""imp CLI entrypoint.

Plan 1: minimal commands (new, resume, log, status). No streaming, no signals,
no spinner. Plan 3 adds the polished UX layer.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sys
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

import anyio

from imp.cli.init import run_init
from imp.cli.live_driver import LiveDriver
from imp.cli.live_status import LiveStatus
from imp.cli.palette import ansi, should_use_color
from imp.cli.prompt import Prompt
from imp.cli.signals import install_process_group, pause_and_exit
from imp.cli.slash import ask_line_with_slash
from imp.engine import journal, recovery, worktree
from imp.engine.change import Change
from imp.engine.errors import LoadError, LoadErrors
from imp.engine.executor import RunResult, run_change
from imp.engine.naming import random_title
from imp.engine.project_root import find_project_root
from imp.engine.slug import dated_slug, new_change_id
from imp.engine.state import LockHeld, ProjectLock, State

_DEFAULT_NONINTERACTIVE_REPLY = "Proceed using your best judgment."


def make_stdin_reply_fn(default_reply: str) -> Callable[[], Awaitable[str]]:
    """A scripted reply channel for --non-interactive runs (§12): each call reads
    the next stdin line; on EOF it returns `default_reply`. Used for BOTH the
    human-gate input and the interactive-agent ask. No TTY, no prompt_toolkit."""
    async def _reply() -> str:
        # Blocking read: the default (non-cancellable) thread means a stalled read
        # blocks task-group cancellation; callers must close stdin (pipe/redirect
        # EOF) to unblock. The §10/§12 wall-clock retry ceiling (Task 12) is the
        # headless backstop.
        line: str = await anyio.to_thread.run_sync(sys.stdin.readline)
        if line == "":                 # EOF
            return default_reply
        return line.rstrip("\r\n")    # strip both CR and LF for Windows-style endings
    return _reply


# -- end-of-run status lines ------------------------------------------------
# Semantic color + a leading glyph tell you at a glance how a run ended. The
# decoration is gated on `should_use_color` (TTY + NO_COLOR aware), so piped /
# redirected output stays plain and script-parseable. Shared by `new`/`resume`.

def _emit_completed(change: Change, value: str, *, merged: bool) -> None:
    """Run finished cleanly — green, ✓ on the headline."""
    color = should_use_color(sys.stdout)
    mark = "✓ " if color else ""
    if merged:
        print(ansi(f"{mark}Merged {change.id} into {change.base_branch}.", "green",
                   enabled=color))
        print(ansi(f"Change {change.id}: {value}", "green", enabled=color))
    else:
        print(ansi(f"{mark}Change {change.id}: {value}", "green", enabled=color))


def _emit_paused(change: Change, how: str) -> None:
    """Interactive exit (ctrl-c / ctrl-d) — a soft, expected stop: dim, ⏸."""
    color = should_use_color(sys.stdout)
    mark = "⏸ " if color else ""
    print("\n" + ansi(f"{mark}Change {change.id}: paused ({how})", "dim", enabled=color))


def _emit_paused_summary(change: Change, value: str) -> None:
    """The trailing run-status line on a non-interactive pause / merge-block."""
    color = should_use_color(sys.stdout)
    mark = "⏸ " if color else ""
    print(ansi(f"{mark}Change {change.id}: {value}", "dim", enabled=color))


def _emit_merge_blocked(change: Change, reason: str | None) -> None:
    """Merge couldn't complete — the user must act: yellow, ⚠."""
    color = should_use_color(sys.stdout)
    mark = "⚠ " if color else ""
    cid, base = change.id, change.base_branch
    if reason == "worktree_dirty":
        msg = f"Uncommitted work in the worktree; commit it, then `imp resume {cid}`."
    elif reason == "base_dirty":
        msg = (f"Uncommitted changes on `{base}`; commit or stash them, "
               f"then `imp resume {cid}`.")
    elif reason == "conflict":
        msg = (f"`imp/{cid}` doesn't auto-merge into `{base}`. imp can't resolve "
               f"conflicts in this version — merge `imp/{cid}` into `{base}` manually, "
               f"then the Change is done (its worktree at `.imp/worktrees/{cid}` "
               f"can be removed).")
    else:
        msg = f"Change {cid}: merge_blocked. Resolve, then `imp resume {cid}`."
    print(ansi(f"{mark}{msg}", "yellow", enabled=color))


def _project_root() -> Path:
    return find_project_root()


def _control_root() -> Path:
    return worktree.find_control_root(Path.cwd())


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

    change_id = new_change_id(date=datetime.now(UTC).strftime("%Y-%m-%d"))
    title = args.title if args.title is not None else random_title()
    try:
        change = Change.create(control, title=title, change_id=change_id,
                               base_branch=base)
    except (LoadError, LoadErrors) as e:
        print(f"imp: {e}", file=sys.stderr)
        return 2
    except worktree.BranchExists:
        # The random suffix makes this effectively impossible; surface a clean
        # message rather than a raw git error (spec §2, AC 7).
        print(f"imp: a Change with id {change_id!r} already exists; re-run `imp new`",
              file=sys.stderr)
        return 2

    try:
        with ProjectLock(change.change_dir / "lock").acquire():  # per-Change single-runner
            # No "Created Change …" line: the driver's `change_created` event
            # prints the bold `#slug  title` workflow header as the opener, so a
            # second headline here would just duplicate it.
            non_interactive = bool(getattr(args, "non_interactive", False))
            status = LiveStatus(sys.stdout)
            driver = LiveDriver(stream=sys.stdout,
                                color=should_use_color(sys.stdout),
                                width=_term_width(), status=status)
            coord = InterruptCoordinator()
            rc = 0
            if non_interactive:
                _ask_line = make_stdin_reply_fn(args.default_reply)
            else:
                prompt = Prompt(skills_dir=change.workflow_dir / "skills",
                                editing_mode=_editing_mode(change.control_root),
                                color=should_use_color(sys.stdout))

                async def _ask_line() -> str:
                    return await ask_line_with_slash(
                        prompt, status, change,
                        color=should_use_color(sys.stdout), width=_term_width())

            async with anyio.create_task_group() as tg:
                tg.start_soon(coord.run_signal_receiver)
                if not non_interactive:
                    tg.start_soon(status.tick)
                try:
                    result = await run_change(
                        change,
                        on_event=driver.on_event,
                        human_input_fn=_ask_line,
                        human_output_fn=driver.print_chrome,
                        user_chat_input_fn=lambda _q: _ask_line(),
                        coordinator=coord,
                        non_interactive=non_interactive,
                    )
                except (LoadError, LoadErrors) as e:
                    driver.finish()
                    print(f"imp: {e}", file=sys.stderr)
                    rc = 2
                except EOFError:
                    driver.finish()
                    pause_and_exit(change, reason="ctrl-d")
                    _emit_paused(change, "ctrl-d")
                    rc = 130
                except KeyboardInterrupt:
                    # prompt_toolkit's prompt_async (handle_sigint=True) installs
                    # its own SIGINT handler and raises KeyboardInterrupt while the
                    # interactive prompt is active, bypassing InterruptCoordinator.
                    # Catch it here, mirroring EOFError, so Ctrl-C at the prompt
                    # pauses instead of escaping the task group as a crash.
                    driver.finish()
                    pause_and_exit(change, reason="ctrl-c")
                    _emit_paused(change, "ctrl-c")
                    rc = 130
                else:
                    driver.finish()
                    if coord.should_pause:
                        pause_and_exit(change, reason="ctrl-c")
                        _emit_paused(change, "ctrl-c")
                        rc = 130
                    else:
                        if result is RunResult.COMPLETED:
                            # Worktree gone = the merge gate was exercised.
                            wt_path = change.control_root / ".imp" / "worktrees" / change.id
                            _emit_completed(change, result.value,
                                            merged=not wt_path.exists())
                            rc = 0
                        else:
                            # PAUSED — surface merge_blocked remediation, if any.
                            final_state = State.load(change.state_dir)
                            if final_state.status == "merge_blocked":
                                reason = None
                                for ev in change.event_log().read():
                                    if ev.get("type") == "merge_blocked":
                                        reason = (ev.get("payload") or {}).get("reason")
                                _emit_merge_blocked(change, reason)
                            _emit_paused_summary(change, result.value)
                            rc = 1
                finally:
                    status.reset()
                    tg.cancel_scope.cancel()
            return rc
    except LockHeld as e:
        print(f"imp: Change {change.id} is already running (pid {e.held_by_pid})",
              file=sys.stderr)
        return 2


def _cmd_new(args: argparse.Namespace) -> int:
    return anyio.run(_async_cmd_new, args)


async def _async_cmd_resume(args: argparse.Namespace) -> int:
    install_process_group()
    from imp.cli.signals import InterruptCoordinator

    control = _control_root()
    try:
        change_id = _resolve_id(control, args.token)
    except SystemExit:
        return 2
    try:
        change = Change.load(control, change_id)
    except (LoadError, LoadErrors) as err:
        print(f"imp: {err}", file=sys.stderr)
        return 2
    state = State.load(change.state_dir)
    if state.status not in ("paused", "errored", "merge_blocked"):
        print(
            f"imp: Change {change.id} status is {state.status!r}; "
            "nothing to resume",
            file=sys.stderr,
        )
        return 2
    try:
        with ProjectLock(change.change_dir / "lock").acquire():  # per-Change single-runner
            # Re-read status under the lock: the precondition above is an
            # unlocked read, so a concurrent `imp abandon` (which sets
            # status="abandoned", journals, then releases the lock before
            # removing the worktree) could have landed in the gap. Re-checking
            # here ensures resume never runs a Change abandon has finalized.
            state = State.load(change.state_dir)
            if state.status not in ("paused", "errored", "merge_blocked"):
                print(
                    f"imp: Change {change.id} status is {state.status!r}; "
                    "nothing to resume",
                    file=sys.stderr,
                )
                return 2
            # Resume gets no `change_created` event, so the driver won't print the
            # workflow header — emit a matching one here (bold `#slug  title`) with
            # a dim resuming marker, so a resumed run opens like a fresh one. A
            # title-less change defers its header too (Option B), matching new.
            _color = should_use_color(sys.stdout)
            if change.title and change.title.strip():
                print(ansi(f"#{change.slug}  {change.title}", "bold", "magenta", enabled=_color)
                      + ansi(" · resuming", "dim", enabled=_color))

            # spec 2026-06-04: --from rewind preflight, before any run. Resolve
            # + gate-check FIRST (plan_rewind raises before mutating/emitting) so
            # a refused --from leaves zero new events. The status precondition
            # above still gates this — --from cannot bypass it.
            from_target = getattr(args, "from_target", None)
            rewind_plan = None
            workflow = None
            if from_target is not None:
                from imp.engine.loader import load_workflow
                from imp.providers.config import load_config
                try:
                    cfg = load_config(change.control_root)
                    workflow = load_workflow(
                        change.workflow_dir, configured_tiers=cfg.tier_names())
                except (LoadError, LoadErrors) as e:
                    print(f"imp: {e}", file=sys.stderr)
                    return 2
                try:
                    rewind_plan = recovery.plan_rewind(
                        workflow, state, from_target,
                        force=bool(getattr(args, "force", False)))
                except (recovery.RewindError, recovery.GateBypassRequired) as e:
                    # str(e) bare (not the usual f"imp: {e}"): these messages
                    # already embed the "imp:" prefix and are multi-line (they
                    # list valid targets / skipped gates), so they are printed
                    # verbatim rather than re-prefixed.
                    print(str(e), file=sys.stderr)
                    return 2

            non_interactive = bool(getattr(args, "non_interactive", False))
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
            # Apply the rewind AFTER the resume marker so step_rewound (+ any
            # gates_bypassed) follow workflow_resumed in the append-only log.
            if rewind_plan is not None:
                # workflow is set whenever rewind_plan is (both assigned only
                # inside `if from_target is not None`); assert narrows for mypy.
                assert workflow is not None
                recovery.apply_rewind(
                    rewind_plan, workflow, state, change.event_log())
            state.save(change.state_dir)

            if non_interactive:
                _ask_line = make_stdin_reply_fn(args.default_reply)
            else:
                prompt = Prompt(skills_dir=change.workflow_dir / "skills",
                                editing_mode=_editing_mode(change.control_root),
                                color=should_use_color(sys.stdout))

                async def _ask_line() -> str:
                    return await ask_line_with_slash(
                        prompt, status, change,
                        color=should_use_color(sys.stdout), width=_term_width())

            async with anyio.create_task_group() as tg:
                tg.start_soon(coord.run_signal_receiver)
                if not non_interactive:
                    tg.start_soon(status.tick)
                try:
                    result = await run_change(
                        change,
                        on_event=driver.on_event,
                        human_input_fn=_ask_line,
                        human_output_fn=driver.print_chrome,
                        user_chat_input_fn=lambda _q: _ask_line(),
                        coordinator=coord,
                        non_interactive=non_interactive,
                    )
                except (LoadError, LoadErrors) as e:
                    driver.finish()
                    print(f"imp: {e}", file=sys.stderr)
                    rc = 2
                except EOFError:
                    driver.finish()
                    pause_and_exit(change, reason="ctrl-d")
                    _emit_paused(change, "ctrl-d")
                    rc = 130
                except KeyboardInterrupt:
                    # prompt_toolkit's prompt_async (handle_sigint=True) installs
                    # its own SIGINT handler and raises KeyboardInterrupt while the
                    # interactive prompt is active, bypassing InterruptCoordinator.
                    # Catch it here, mirroring EOFError, so Ctrl-C at the prompt
                    # pauses instead of escaping the task group as a crash.
                    driver.finish()
                    pause_and_exit(change, reason="ctrl-c")
                    _emit_paused(change, "ctrl-c")
                    rc = 130
                else:
                    driver.finish()
                    if coord.should_pause:
                        pause_and_exit(change, reason="ctrl-c")
                        _emit_paused(change, "ctrl-c")
                        rc = 130
                    else:
                        if result is RunResult.COMPLETED:
                            # Worktree gone = the merge gate was exercised.
                            wt_path = change.control_root / ".imp" / "worktrees" / change.id
                            _emit_completed(change, result.value,
                                            merged=not wt_path.exists())
                            rc = 0
                        else:
                            # PAUSED — surface merge_blocked remediation, if any.
                            final_state = State.load(change.state_dir)
                            if final_state.status == "merge_blocked":
                                reason = None
                                for ev in change.event_log().read():
                                    if ev.get("type") == "merge_blocked":
                                        reason = (ev.get("payload") or {}).get("reason")
                                _emit_merge_blocked(change, reason)
                            _emit_paused_summary(change, result.value)
                            rc = 1
                finally:
                    status.reset()
                    tg.cancel_scope.cancel()
            return rc
    except LockHeld as e:
        print(f"imp: Change {change.id} is already running (pid {e.held_by_pid})",
              file=sys.stderr)
        return 2


def _cmd_resume(args: argparse.Namespace) -> int:
    return anyio.run(_async_cmd_resume, args)


def _editing_mode(control_root: Path) -> str:
    """Read editing_mode from config; default to emacs if config is absent or
    invalid (run_change reports a bad config with full context — don't double-fail
    here just to pick a prompt mode)."""
    from imp.providers.config import load_config
    try:
        return load_config(control_root).editing_mode
    except (LoadError, LoadErrors):
        return "emacs"


def _term_width() -> int:
    # shutil.get_terminal_size honors $COLUMNS and substitutes the fallback when
    # the OS reports columns <= 0 (e.g. a winsize-0 PTY), unlike
    # os.get_terminal_size which returns a literal 0 and floors wrapping to 8
    # chars (issue 2). Defense-in-depth with the dogfood PTY winsize fix.
    return shutil.get_terminal_size((80, 24)).columns


def _list_change_worktrees(control: Path) -> list[str]:
    """Ids of in-flight Change worktrees under .imp/worktrees/ (excludes the
    transient .merge-* scratch dirs)."""
    root = control / ".imp" / "worktrees"
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir()
                  if p.is_dir() and not p.name.startswith(".")
                  and (p / ".imp" / "change" / "change.json").exists())


def _iter_change_ids(control: Path) -> list[str]:
    """All known Change ids: live worktrees first, then journaled ids not live."""
    live = _list_change_worktrees(control)
    seen = set(live)
    out = list(live)
    for cid in journal.journal_ids(control):
        if cid not in seen:
            out.append(cid)
            seen.add(cid)
    return out


def _title_for_id(control: Path, change_id: str) -> str | None:
    """Read a Change's current title (worktree first, journal second)."""
    cdir = control / ".imp" / "worktrees" / change_id / ".imp" / "change"
    cj = cdir / "change.json"
    if cj.exists():
        try:
            return str(json.loads(cj.read_bytes())["title"])
        except (json.JSONDecodeError, KeyError):
            return None
    raw = journal.journal_show(control, change_id, "change.json")
    if raw is None:
        return None
    try:
        return str(json.loads(raw)["title"])
    except (json.JSONDecodeError, KeyError):
        return None


def _resolve_id(control: Path, token: str) -> str:
    """Resolve a user-typed token to a Change id (spec §5). Precedence:
      1. exact id
      2. unique id SUFFIX match (the 6-char random tail; `imp resume k3f9qa`)
      3. exact current-slug match (slug derived from the live title)
    Ambiguous at any level -> exit non-zero listing candidate ids. Prefix
    matching is intentionally NOT attempted (all same-day ids share YYYY-MM-DD-)."""
    ids = _iter_change_ids(control)

    if token in ids:                                       # (1) exact id
        return token

    suffix_hits = [cid for cid in ids if cid.endswith(token) and cid != token]
    if len(suffix_hits) == 1:                              # (2) unique suffix
        return suffix_hits[0]
    if len(suffix_hits) > 1:
        _exit_ambiguous(token, suffix_hits)

    slug_hits = []
    for cid in ids:
        title = _title_for_id(control, cid)
        if title is None:
            continue
        try:
            slug = dated_slug(title, date=cid[:10])
        except Exception:
            continue
        if slug == token:
            slug_hits.append(cid)
    if len(slug_hits) == 1:                                 # (3) current slug
        return slug_hits[0]
    if len(slug_hits) > 1:
        _exit_ambiguous(token, slug_hits)

    # Not resolvable. A bare prefix shared by several ids (e.g. `2026-06-04`) is
    # rejected as AMBIGUOUS and lists the candidates (spec §5 / AC 5): prefixes
    # are never *resolved*, but a shared prefix yields a helpful candidate list
    # rather than a blank "no match", so the user can retype with a suffix.
    prefix_hits = [cid for cid in ids if cid.startswith(token)]
    if len(prefix_hits) > 1:
        _exit_ambiguous(token, prefix_hits)

    print(f"imp: no Change matches {token!r}", file=sys.stderr)
    raise SystemExit(2)


def _exit_ambiguous(token: str, candidates: list[str]) -> NoReturn:
    print(f"imp: {token!r} is ambiguous; candidates:", file=sys.stderr)
    for cid in sorted(candidates):
        print(f"  {cid}", file=sys.stderr)
    raise SystemExit(2)


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


def _load_events_for_id(control: Path, change_id: str) -> list[dict[str, Any]] | None:
    """Worktree first, journal second. Returns None if the id is in neither.

    Worktree-first is necessary, not merely preferred: an id lives in exactly
    one place (journal_commit runs once, concurrent with worktree removal), so
    the only overlap is a crash mid-sequence where worktree-first harmlessly
    returns identical content."""
    try:
        change = Change.load(control, change_id)
    except LoadError:
        change = None
    if change is not None:
        return list(change.event_log().read())
    raw = journal.journal_show(control, change_id, "events.jsonl")
    if raw is None:
        return None
    return _parse_jsonl(raw)


def _resolve_default_id(control: Path) -> str | None:
    """No-arg default: most-recent in-flight worktree, else most-recent
    journaled id (by last-event timestamp), else None."""
    wts = _list_change_worktrees(control)
    if wts:
        return max(wts, key=lambda s: (control / ".imp" / "worktrees" / s
                                       / ".imp" / "change" / "events.jsonl").stat().st_mtime)
    journaled = journal.journal_ids(control)
    if not journaled:
        return None
    return max(journaled, key=lambda s: _journal_last_activity(control, s))


def _cmd_log(args: argparse.Namespace) -> int:
    from imp.cli import narrative
    from imp.cli.render_text import render_tree

    control = _control_root()
    change_id: str | None
    if args.token is not None:
        try:
            change_id = _resolve_id(control, args.token)
        except SystemExit:
            return 2
    else:
        change_id = _resolve_default_id(control)
    if change_id is None:
        print("imp: no Changes found (no in-flight or completed Changes)",
              file=sys.stderr)
        return 2
    events = _load_events_for_id(control, change_id)
    if events is None:
        print(f"imp: no such Change {change_id!r}", file=sys.stderr)
        return 2

    tree = narrative.build_tree(events)
    width = _term_width()
    color = should_use_color(sys.stdout)
    lines = render_tree(tree, full=args.full, color=color, width=width)
    # Lines carry no trailing newline; print() adds one. No pager — straight to stdout.
    for line in lines:
        print(line)
    return 0


def _last_nonempty_line(data: bytes) -> bytes | None:
    """Last non-empty line of `data` with trailing whitespace removed, or None
    when there is no non-empty line. Avoids splitting the whole buffer: a single
    rstrip + rfind, used by the journal path (full blob already in memory) and
    by the filesystem tail reader."""
    stripped = data.rstrip()
    if not stripped:
        return None
    nl = stripped.rfind(b"\n")
    return stripped[nl + 1:]


_TAIL_CHUNK = 64 * 1024


def _read_last_line(events_path: Path) -> bytes | None:
    """Last non-empty line of `events_path`, read from the end without loading
    the whole file. Returns None when the file has no non-empty line. Defined to
    return exactly `_last_nonempty_line(events_path.read_bytes())` — it reads
    backward in 64 KiB chunks only until the last line is fully in hand. Raises
    FileNotFoundError if the file is absent (the caller maps that to '-')."""
    with events_path.open("rb") as f:
        f.seek(0, os.SEEK_END)
        pos = f.tell()
        buf = b""
        while pos > 0:
            step = min(_TAIL_CHUNK, pos)
            pos -= step
            f.seek(pos)
            buf = f.read(step) + buf
            stripped = buf.rstrip()
            if not stripped:
                continue  # only trailing whitespace seen so far
            nl = stripped.rfind(b"\n")
            if nl != -1:
                return stripped[nl + 1:]
            # No newline yet: the last line starts at or before `pos`; if pos
            # has reached 0 the loop ends and the final return handles it.
        return _last_nonempty_line(buf)


def _format_last_timestamp(line: str) -> str:
    """Local-time YYYY-MM-DD HH:MM:SS for a single already-extracted event line,
    or '-' when the line is empty / malformed. Callers pass the last non-empty
    line (see _read_last_line / _last_nonempty_line)."""
    if not line.strip():
        return "-"
    try:
        record = json.loads(line)
        ts = datetime.fromisoformat(record["timestamp"]).astimezone()
        return ts.strftime("%Y-%m-%d %H:%M:%S")
    except (json.JSONDecodeError, KeyError, ValueError):
        return "-"


def _last_event_timestamp(events_path: Path) -> str:
    try:
        line = _read_last_line(events_path)
    except FileNotFoundError:
        return "-"
    if line is None:
        return "-"
    return _format_last_timestamp(line.decode("utf-8", errors="replace"))


def _journal_last_activity(control: Path, change_id: str) -> str:
    raw = journal.journal_show(control, change_id, "events.jsonl")
    if raw is None:
        return "-"
    line = _last_nonempty_line(raw)
    if line is None:
        return "-"
    return _format_last_timestamp(line.decode("utf-8", errors="replace"))


# Spec §10: in-flight statuses; completed Changes live in the journal.
_INFLIGHT = ("running", "paused", "merge_blocked")


def _status_row(change_id: str, state_json: bytes, change_json: bytes,
                last_activity: str) -> tuple[str, str, str, str, str] | None:
    """Pure (id, status, step, last_activity, title) extractor, fed by either
    worktree file bytes or journal_show bytes. The journaled state.json and
    change.json are schema-identical to the in-flight ones (both written by
    State.save / Change.create), so the two sections can't silently drift.
    Returns None on malformed/incomplete JSON so a single corrupt entry can't
    crash the whole `imp status` listing (callers skip None)."""
    try:
        st = json.loads(state_json)
        meta = json.loads(change_json)
        return (change_id, st["status"], st.get("step") or "-", last_activity, meta["title"])
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def _status_rows(control: Path) -> list[tuple[str, str, str, str, str]]:
    rows: list[tuple[str, str, str, str, str]] = []
    for change_id in _list_change_worktrees(control):
        cdir = control / ".imp" / "worktrees" / change_id / ".imp" / "change"
        state_json = (cdir / "state" / "state.json").read_bytes()
        change_json = (cdir / "change.json").read_bytes()
        last = _last_event_timestamp(cdir / "events.jsonl")
        row = _status_row(change_id, state_json, change_json, last)
        if row is None or row[1] not in _INFLIGHT:
            continue
        rows.append(row)
    return rows


def _completed_rows(control: Path) -> list[tuple[str, str, str, str, str]]:
    """Journal-backed completed Changes: every journaled id NOT present as a
    live worktree (in-flight takes precedence on the crash-window overlap)."""
    live = set(_list_change_worktrees(control))
    rows: list[tuple[str, str, str, str, str]] = []
    for change_id in journal.journal_ids(control):
        if change_id in live:
            continue
        state_json = journal.journal_show(control, change_id, "state.json")
        change_json = journal.journal_show(control, change_id, "change.json")
        if state_json is None or change_json is None:
            continue
        last = _journal_last_activity(control, change_id)
        row = _status_row(change_id, state_json, change_json, last)
        if row is None or row[1] in _INFLIGHT:
            continue  # belt-and-suspenders; journaled rows are completed
        rows.append(row)
    return rows


# STATUS cell colors mirror imp log's restraint — color only this cell,
# leave the rest plain. Padding is computed against the un-colored status
# text so the column edge holds when colors are on.
_STATUS_COLORS: dict[str, tuple[str, ...]] = {
    "running": ("green",),
    "paused": ("cyan",),
    "merge_blocked": ("yellow",),
    "errored": ("bold", "red"),
    "completed": ("gray",),
}


def _render_status_table(
    rows: list[tuple[str, str, str, str, str]],
    *,
    color_enabled: bool,
) -> list[str]:
    """Format pre-sorted rows into aligned table lines. Per-column widths are
    `max(len(header), max(len(cell) for cell in column))` over the un-colored
    cell text; only the STATUS value (not its trailing pad) is wrapped in
    ANSI, so later columns hold their offset whether color is on or off."""
    headers = ("ID", "STATUS", "STEP", "LAST_ACTIVITY", "TITLE")
    # Compute widths for columns 0-3 only; TITLE is the terminal column and
    # needs no padding.
    widths = [
        max(len(headers[i]), max((len(r[i]) for r in rows), default=0))
        for i in range(4)
    ]
    lines: list[str] = [
        f"{headers[0]:<{widths[0]}} "
        f"{headers[1]:<{widths[1]}} "
        f"{headers[2]:<{widths[2]}} "
        f"{headers[3]:<{widths[3]}} "
        f"{headers[4]}"
    ]
    for slug, status, step, last, title in rows:
        status_cell = f"{status:<{widths[1]}}"
        codes = _STATUS_COLORS.get(status, ())
        if color_enabled and codes:
            pad = " " * (widths[1] - len(status))
            status_cell = ansi(status, *codes, enabled=True) + pad
        lines.append(
            f"{slug:<{widths[0]}} "
            f"{status_cell} "
            f"{step:<{widths[2]}} "
            f"{last:<{widths[3]}} "
            f"{title}"
        )
    return lines


def _abandon(control: Path, change_id: str) -> int:
    """Tear down a Change (worktree, branch, state) as one logged operation.

    Order is pinned by git (spec §2): journal BEFORE worktree removal (records
    live inside the worktree); `git branch -D` AFTER (git refuses to delete a
    branch checked out in a worktree). Idempotent / crash-tolerant: re-running
    after any mid-sequence crash converges."""
    try:
        change = Change.load(control, change_id)
    except (LoadError, LoadErrors):
        # Worktree fully gone (e.g. a crash between worktree removal and branch
        # deletion). Idempotent teardown: reap a possibly-orphaned branch (the
        # name needs only the id) and exit 0 — NOT the rc=2 LoadError path.
        worktree.delete_branch(control, change_id)
        print(f"Change {change_id} already gone; ensured branch imp/{change_id} is removed.")
        return 0

    try:
        with ProjectLock(change.change_dir / "lock").acquire():  # timeout=0
            state = State.load(change.state_dir)
            state.status = "abandoned"
            state.save(change.state_dir)                          # (a)
            change.event_log().append(                            # (b)
                agent_id=None, type="change_abandoned", payload={})
            worktree.journal_commit(control, change_id, {         # (c)
                "events.jsonl": change.events_path,
                "change.json": change.change_dir / "change.json",
                "state.json": change.state_dir / "state.json",
            })
        # lock released here (after c, before d): nothing below needs it, and
        # the persisted status="abandoned" blocks any concurrent resume —
        # _async_cmd_resume re-reads status under the lock and refuses.
    except LockHeld as e:
        print(f"imp: Change {change_id} is running (pid {e.held_by_pid or '?'}); "
              f"stop it before abandoning", file=sys.stderr)
        return 2

    worktree.remove_worktree(control, change_id, force=True)      # (d)
    worktree.delete_branch(control, change_id)                    # (e)
    print(f"Abandoned {change_id}.")
    return 0


def _cmd_abandon(args: argparse.Namespace) -> int:
    control = _control_root()
    try:
        change_id = _resolve_id(control, args.token)
    except SystemExit:
        # _resolve_id scans live worktrees only. A Change whose worktree was
        # already removed (crash between abandon's remove and branch-delete)
        # is unresolvable there — but abandon journaled the id before removal,
        # so recover it from the journal to still reap an orphaned branch
        # (AC #12). Exact id, else unique suffix match.
        ids = journal.journal_ids(control)
        token = args.token
        hits = [cid for cid in ids if cid == token] or \
               [cid for cid in ids if cid.endswith(token)]
        if len(hits) == 1:
            return _abandon(control, hits[0])
        # Truly unknown (never existed, or ambiguous) — nothing to reap.
        print(f"imp: no Change matches {token!r}", file=sys.stderr)
        return 2
    return _abandon(control, change_id)


def _cmd_status(args: argparse.Namespace) -> int:
    try:
        control = _control_root()
    except worktree.PreconditionError:
        print("No Changes.")
        return 0
    rows = _status_rows(control) + _completed_rows(control)
    if not rows:
        print("No Changes.")
        return 0
    # Sort rows by `last_activity` descending so the most-recently changed
    # entry appears first. `last_activity` is YYYY-MM-DD HH:MM:SS or '-';
    # lexicographic DESC puts real timestamps before '-' because '-' (0x2D)
    # is less than '2' (0x32), so '-' entries fall to the bottom.
    rows.sort(key=lambda r: r[3], reverse=True)
    color_enabled = should_use_color(sys.stdout)
    for line in _render_status_table(rows, color_enabled=color_enabled):
        print(line)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(prog="imp")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init")
    p_init.set_defaults(func=_cmd_init)

    p_new = sub.add_parser("new")
    p_new.add_argument("title", nargs="?")
    p_new.add_argument("--non-interactive", action="store_true",
                       help="Headless: read replies from stdin, default on EOF, no live chrome.")
    p_new.add_argument("--default-reply", default=_DEFAULT_NONINTERACTIVE_REPLY,
                       help="Reply used when stdin reaches EOF (non-interactive).")
    p_new.set_defaults(func=_cmd_new)

    p_resume = sub.add_parser("resume")
    p_resume.add_argument("token")
    p_resume.add_argument("--non-interactive", action="store_true",
                          help="Headless: read replies from stdin, default on EOF, no live chrome.")
    p_resume.add_argument("--default-reply", default=_DEFAULT_NONINTERACTIVE_REPLY,
                          help="Reply used when stdin reaches EOF (non-interactive).")
    p_resume.add_argument(
        "--from", dest="from_target", metavar="TARGET",
        help="Rewind/jump the step cursor to TARGET (a step or phase name) and "
             "re-run forward, appending new events (never rewriting history).")
    p_resume.add_argument(
        "--force", action="store_true",
        help="Permit a forward --from jump that skips gate steps "
             "(land the Change as-is; sets ready_to_merge).")
    p_resume.set_defaults(func=_cmd_resume)

    p_log = sub.add_parser(
        "log",
        help="Show the narrative for a Change.",
        description=(
            "Show the narrative for a Change as plain text. --full starts every "
            "fold expanded. Set NO_COLOR=1 to disable colors. "
            "Tip: imp log <id> --full | grep ... for line-oriented scripting."
        ),
    )
    p_log.add_argument("token", nargs="?")
    p_log.add_argument(
        "--full", action="store_true",
        help="Render every event (no tool-burst collapse).",
    )
    p_log.set_defaults(func=_cmd_log)

    p_status = sub.add_parser("status")
    p_status.set_defaults(func=_cmd_status)

    p_abandon = sub.add_parser(
        "abandon",
        help="Tear down a Change's worktree, branch, and state (logged).",
    )
    p_abandon.add_argument("token")
    p_abandon.set_defaults(func=_cmd_abandon)

    from imp.evals.cli import add_eval_parser

    add_eval_parser(sub)

    ns = parser.parse_args(args)
    try:
        rc: int = ns.func(ns)
        # Flush here, inside the guard: output small enough to fit Python's
        # stdout buffer is otherwise flushed at interpreter shutdown, where a
        # broken pipe surfaces as an uncatchable "Exception ignored" traceback.
        # Forcing the flush now moves that write where the handler can catch it.
        sys.stdout.flush()
        return rc
    except BrokenPipeError:
        # A reader (e.g. `imp log | less` quit before draining) closed the pipe.
        # Redirect stdout to devnull so the interpreter's final flush can't
        # re-raise during shutdown, then exit with the conventional SIGPIPE code.
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        return 128 + signal.SIGPIPE
