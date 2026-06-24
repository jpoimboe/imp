"""imp CLI entrypoint.

Plan 1: minimal commands (new, resume, log, status). No streaming, no signals,
no spinner. Plan 3 adds the polished UX layer.
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import json
import logging
import os
import shlex
import shutil
import signal
import subprocess
import sys
from collections.abc import Awaitable, Callable, Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple, NoReturn

import anyio

from imp import __version__
from imp.agents.tools.wait_for_user import AwaitingUserInput
from imp.cli import change_row, playbooks, resume_select, splash
from imp.cli import palette as pal
from imp.cli.init import GitSetupDeclined, run_init
from imp.cli.live_driver import LiveDriver
from imp.cli.live_status import LiveStatus
from imp.cli.palette import ansi, should_use_color
from imp.cli.prompt import Prompt
from imp.cli.session_run import (
    infer_session_result,
    make_headless_solicit,
    make_interactive_solicit,
    run_session_client,
)
from imp.cli.session_terminal import run_terminal_client
from imp.cli.signals import install_process_group
from imp.cli.tui_app import TuiApp
from imp.engine import backlog, worktree
from imp.engine.abandon import (
    archive_conversation,
    discard_conversation,
    save_to_backlog,
    tear_down_change,
)
from imp.engine.archive import read as archive_read
from imp.engine.change import Change
from imp.engine.change_id import new_change_id
from imp.engine.errors import LayoutMismatch, LoadError, LoadErrors
from imp.engine.executor import RunResult
from imp.engine.project_root import find_project_root
from imp.engine.state import LockHeld, State
from imp.providers.config import parse_default_workflow, validate_workflow_name
from imp.remote.daemon.directory import session_is_resident
from imp.remote.session.attach import build_session_attach


class ReplyInterjectRouter:
    """Routes each human-typed line by the agent's state AT SUBMIT TIME (F7/R6).

    `route(line)` is the pure decision:
      - solicit ACTIVE (the engine is blocked in a `wait_for_user`,
        a prompt is up) → the line is the REPLY → delivered to `reply_sink`;
      - solicit INACTIVE (the agent is running) → the line is an INTERJECT →
        queued on the coordinator, delivered at the next agent turn boundary.

    `begin_solicit()` / `end_solicit()` toggle the flag around the prompt read;
    `solicit_active` exposes it. This object is the unit-testable routing CORE
    (tests/test_cli_interject_routing.py) — it touches no TTY.

    In the LIVE CLI the reply itself comes straight from the prompt_toolkit read
    (so `reply_sink` is rarely exercised). The flag still guards the race where
    a sink call lands during a prompt — that line is then a reply, not a stray
    interject.

    Slash interception is scoped to the REPLY path only: when a solicit is
    active the line reader (`ask_line_with_slash`) consumes `/help`/`/skills`
    (print + re-prompt) and resolves `/<skill>` to its body before the line
    reaches `route`. On the INTERJECT path (a line typed mid-run, solicit
    inactive) there is NO slash interception — the raw line is enqueued verbatim
    and delivered to the agent as a user message. Loading a skill via `/<name>`
    mid-run is therefore not supported (do it at the next solicit boundary); a
    mid-run `/...` line reaches the agent as literal text. This is a deliberate
    scope boundary: intercepting slash on the interject path would require
    coupling the Change/event-log and a status-suspending print into the raw-tty
    reader, on the one path already known to CPR-degrade.
    """

    def __init__(
        self, coordinator: Any, *, reply_sink: Callable[[str], None] | None = None,
    ) -> None:
        self._coord = coordinator
        self._reply_sink = reply_sink
        self._solicit_active = False

    @property
    def solicit_active(self) -> bool:
        return self._solicit_active

    def begin_solicit(self) -> None:
        self._solicit_active = True

    def end_solicit(self) -> None:
        self._solicit_active = False

    def route(self, line: str) -> None:
        """Route ONE submitted line by the live solicit-active flag. Reply when a
        solicit is in flight (deliver to reply_sink if set); otherwise enqueue an
        interject. Pure dispatch — no TTY, no blocking; the unit-test authority."""
        if self._solicit_active:
            if self._reply_sink is not None:
                self._reply_sink(line)
        else:
            self._coord.enqueue_interject(line)


SPEC_SEED_MARKER = "Implement this spec"

OPENING_HINT = "type a message, or /help for commands"


def _should_prompt_opening(
    *, seed_text: str | None, interactive: bool,
) -> bool:
    if not interactive:
        return False
    return seed_text is None


def _normalize_opening_reply(reply: str) -> str | None:
    return reply.strip() or None


def _is_interactive(
    *, non_interactive: bool, stdin: Any = None, stdout: Any = None,
) -> bool:
    """An interactive entry: not flagged off, both streams are ttys, and color
    is on. The splash banner and the opening prompt both gate on this so the
    no-color / non-interactive paths show neither (spec AC6)."""
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    return (not non_interactive
            and getattr(stdin, "isatty", lambda: False)()
            and getattr(stdout, "isatty", lambda: False)()
            and should_use_color(stdout))


def _print_splash_banner(*, interactive: bool, stream: Any = None) -> bool:
    """Leading blank line then the splash scene, printed on an interactive entry
    only. Shared by new/chat and resume so the banner is identical everywhere.
    Returns True only when the banner was actually printed (interactive and a
    non-empty scene), so the opening flow can tie its separator to it."""
    if not interactive:
        return False
    out = sys.stdout if stream is None else stream
    block = splash.splash_block(theme=pal.active_theme(), width=_term_width(), color=True)
    if not block:
        return False
    print(file=out)
    print(block, file=out)
    return True


class SeedError(Exception):
    """A bad spec-seed source: a missing/unreadable --spec-file, or two seed
    sources combined."""


class SeedResolution(NamedTuple):
    title: str | None
    seed_text: str | None
    opening_intent: str | None = None


def _read_seed_file(path: str) -> str:
    p = Path(path)
    if not p.is_file():
        raise SeedError(
            f"spec file {path!r} does not exist or is not a regular file")
    try:
        return p.read_text(encoding="utf-8")
    except (OSError, ValueError) as e:
        raise SeedError(f"cannot read spec file {path!r}: {e}") from e


def _build_seed(path: str, content: str) -> str:
    return (f"{SPEC_SEED_MARKER} (the user provided it as a draft at "
            f"`{path}`):\n\n{content}")


def resolve_spec_seed(args: argparse.Namespace) -> SeedResolution:
    """Resolve the first-turn seed source for `imp new`.

    A positional MESSAGE is the user's verbatim opening intent (delivered
    unwrapped); --spec-file delivers a wrapped draft. The two are mutually
    exclusive. Raises SeedError on a missing/unreadable --spec-file or combined
    seed sources."""
    title = getattr(args, "title", None)
    opening = getattr(args, "opening", None)
    spec_file = getattr(args, "spec_file", None)
    message = getattr(args, "message", None)

    if spec_file is not None:
        if opening is not None:
            raise SeedError(
                "a positional MESSAGE and --spec-file are mutually exclusive")
        if message is not None:
            raise SeedError("--spec-file and --message are mutually exclusive")
        return SeedResolution(
            title, _build_seed(spec_file, _read_seed_file(spec_file)),
            opening_intent=None)

    if opening is not None and opening.strip():
        return SeedResolution(title, opening, opening_intent=opening)

    return SeedResolution(title, None, opening_intent=None)


def resolve_playbook_seed(
    control: Path, name: str, args_text: str | None, *, findings_only: bool,
) -> str:
    pb = playbooks.find_playbook(control, name)
    if pb is None:
        available = ", ".join(p.name for p in playbooks.list_playbooks(control))
        raise SeedError(f"unknown playbook {name!r}; available: {available}")
    seed = playbooks.render_playbook(pb, args_text or "")
    if findings_only:
        seed += playbooks.FINDINGS_ONLY_DIRECTIVE
    return seed


def make_message_reply_fn(message: str | None) -> Callable[[], Awaitable[str]]:
    """Headless reply channel: yield the single --message once, then pause.

    The first wait_for_user solicit (live opener OR the dangling wait_for_user
    resolved on resume) consumes `message`; any later solicit in the same
    invocation raises AwaitingUserInput, unwinding to a clean pause the operator
    continues with another `imp resume <id> --message "..."`."""
    delivered = False

    async def _reply(*_a: object, **_k: object) -> str:
        nonlocal delivered
        if message is not None and not delivered:
            delivered = True
            return message
        raise AwaitingUserInput()

    return _reply


# -- end-of-run status lines ------------------------------------------------
# Semantic color + a leading glyph tell you at a glance how a run ended. The
# decoration is gated on `should_use_color` (TTY + NO_COLOR aware), so piped /
# redirected output stays plain and script-parseable. Shared by `new`/`resume`.

def _emit_completed(change: Change, value: str, *, merged: bool) -> None:
    """Run finished cleanly — ✓ on the headline."""
    color = should_use_color(sys.stdout)
    mark = "✓ " if color else ""
    if merged:
        print(ansi(f"{mark}Merged {change.id} into {change.base_branch}.",
                   *pal.NODE_COLORS["lifecycle"], enabled=color))
        print(ansi(f"Change {change.id}: {value}",
                   *pal.NODE_COLORS["lifecycle"], enabled=color))
    else:
        print(ansi(f"{mark}Change {change.id}: {value}", "green", enabled=color))


def _phase_from_events(events: list[dict[str, Any]]) -> str | None:
    """The current phase: the phase of the most recent step_started event."""
    phase: str | None = None
    for e in events:
        if e.get("type") == "step_started":
            phase = (e.get("payload") or {}).get("phase") or phase
    return phase


def _pause_lines(
    name: str, phase: str | None, token: str,
    *, reason: str | None = None, color: bool,
) -> list[str]:
    at = f"  at {phase}" if phase else ""
    why = f"  ({reason})" if reason else ""
    glyph = "⏸ " if color else ""
    return [
        ansi(f"{glyph}Paused  {name}{at}{why}", *pal.NODE_COLORS["lifecycle"],
             enabled=color),
        ansi(f"   resume:  imp resume {token}", *pal.NODE_COLORS["lifecycle"],
             enabled=color),
    ]


def _pause_facts(change: Change) -> tuple[str, str | None, str]:
    name = change.id
    phase = _phase_from_events(list(change.event_log().read()))
    return name, phase, change.id


def _emit_paused_summary(change: Change) -> None:
    """The trailing run-status line on a non-interactive pause / merge-block."""
    color = should_use_color(sys.stdout)
    name, phase, token = _pause_facts(change)
    for ln in _pause_lines(name, phase, token, color=color):
        print(ln)


def _emit_merge_blocked(
    change: Change, reason: str | None, detail: str | None = None,
) -> None:
    """Merge couldn't complete — the user must act: yellow, ⚠.

    `detail` is git's own stderr (carried on the `merge_blocked` event). The
    `refused` and `conflict` reasons are neutral buckets — REFUSED alone covers
    overwrite, unrelated histories, ref-lock, and object-store corruption — so the
    template can only describe the common case. We append git's actual words for
    those two so the surfaced reason is truthful for every refusal, not just the
    overwrite one. `worktree_dirty` has a precise template and gets no detail."""
    color = should_use_color(sys.stdout)
    mark = "⚠ " if color else ""
    cid, base = change.id, change.base_branch
    if reason == "worktree_dirty":
        msg = f"Uncommitted work in the worktree; commit it, then `imp resume {cid}`."
    elif reason == "refused":
        msg = (f"git refused to merge `imp/{cid}` into `{base}` (commonly: uncommitted "
               f"changes on `{base}` that the merge would overwrite). Resolve, then "
               f"`imp resume {cid}`.")
    elif reason == "conflict":
        msg = (f"`imp/{cid}` doesn't auto-merge into `{base}`. imp can't resolve "
               f"conflicts in this version — merge `imp/{cid}` into `{base}` manually, "
               f"then the Change is done (its worktree at `.imp/worktrees/{cid}` "
               f"can be removed).")
    else:
        msg = f"Change {cid}: merge_blocked. Resolve, then `imp resume {cid}`."
    if detail and reason in ("refused", "conflict"):
        msg = f"{msg}\ngit said: {detail.strip()}"
    print(ansi(f"{mark}{msg}", "yellow", enabled=color))


def _emit_script_blocked(step: str, stderr: str) -> None:
    """A script step paused (e.g. integrate-rebase exit 3/4/5).
    Print its stderr so the recovery text reaches the user."""
    color = should_use_color(sys.stdout)
    mark = "⚠ " if color else ""
    print(ansi(f"{mark}{step} paused:\n{stderr.strip()}", "yellow", enabled=color))


def _last_script_blocked(
    events: list[dict[str, Any]], step: str,
) -> dict[str, Any] | None:
    """Return the LAST script_blocked event whose payload.step == `step`, or None.

    A step may pause, be resumed, and pause again with different stderr.  Iterating
    forward and keeping only the last match ensures we surface the most-recent
    block reason and never emit stale output from an earlier pause that was
    subsequently resumed."""
    matched: dict[str, Any] | None = None
    for ev in events:
        if (ev.get("type") == "script_blocked"
                and (ev.get("payload") or {}).get("step") == step):
            matched = ev
    return matched


def _handle_terminal_outcome(
    change: Change, result: RunResult, coord: Any,
) -> int:
    """Render the terminal outcome of a settled run and return its rc.

    COMPLETED → green completion line (rc 0). PAUSED → merge_blocked exits early (no summary);
    script-step remediation surfaced once, then the dim paused summary (rc 1).
    Shared by `new`/`resume` (relocated verbatim from the post-run `else` arm)."""
    if result is RunResult.COMPLETED:
        # Worktree gone = the merge gate was exercised.
        wt_path = change.control_root / ".imp" / "worktrees" / change.id
        _emit_completed(change, result.value, merged=not wt_path.exists())
        return 0
    # PAUSED — surface merge_blocked or script-step remediation.
    final_state = State.load(change.state_dir)
    if final_state.status == "merge_blocked":
        mb_reason: str | None = None
        mb_detail: str | None = None
        for ev in change.event_log().read():
            if ev.get("type") == "merge_blocked":
                payload = ev.get("payload") or {}
                mb_reason = payload.get("reason")
                mb_detail = payload.get("detail")
        _emit_merge_blocked(change, mb_reason, mb_detail)
        return 1  # friendly block already carries the resume command; skip redundant summary
    if final_state.step is not None:
        matched = _last_script_blocked(
            list(change.event_log().read()), final_state.step)
        if matched is not None:
            _emit_script_blocked(
                final_state.step,
                (matched.get("payload") or {}).get("stderr", ""),
            )
    _emit_paused_summary(change)
    return 1


def _had_exchange(log: Any) -> bool:
    """True if the user contributed at least one message to a conversation."""
    events = list(log.read())
    waited = {
        ev.get("payload", {}).get("id")
        for ev in events
        if ev.get("type") == "tool_call"
        and ev.get("payload", {}).get("name") == "wait_for_user"
        and ev.get("payload", {}).get("id") is not None
    }
    for ev in events:
        etype = ev.get("type")
        if etype in ("opening_intent", "user_message", "user_interjected"):
            return True
        if etype == "tool_result" and ev.get("payload", {}).get("id") in waited:
            return True
    return False


async def _apply_disposition_locked(
    fn: Callable[[Change], None], change: Change, *,
    attempts: int = 100, delay: float = 0.1,
) -> None:
    """Apply a lock-taking disposition with retry while the daemon still holds the lock."""
    for _ in range(attempts - 1):
        try:
            await anyio.to_thread.run_sync(fn, change)
            return
        except LockHeld:
            await anyio.sleep(delay)
    await anyio.to_thread.run_sync(fn, change)


async def _maybe_apply_leave_disposition(
    change: Change, leave_mode: str | None, leave_state: dict[str, str],
) -> int | None:
    """Finalize a left phase-0 conversation. Returns 0 when applied, None to defer."""
    if leave_mode is None or leave_state.get("intent") == "abandon":
        return None
    try:
        change = Change.load(change.control_root, change.id)
    except (LoadError, LoadErrors):
        return None  # home gone — defer to the normal path
    if change.materialized:
        return None  # a real in-flight change, not a phase-0 leave
    explicit_save = leave_state.get("intent") == "save"
    try:
        if not explicit_save and not _had_exchange(change.event_log()):
            await _apply_disposition_locked(discard_conversation, change)
            return 0
        if explicit_save or leave_mode == "new":
            await _apply_disposition_locked(save_to_backlog, change)
            print(f"imp: saved '{change.title or change.id}' to your Backlog "
                  f"(resume with `imp resume {change.id}`)")
            return 0
        await _apply_disposition_locked(archive_conversation, change)
        print(f"imp: conversation saved — resume with `imp resume {change.id}` "
              f"or find it later with `imp search`")
        return 0
    except LockHeld:
        print(f"imp: '{change.id}' is still busy and was left running "
              f"(resume with `imp resume {change.id}`)", file=sys.stderr)
        return None


def _session_exit_code(change: Change, coord: Any) -> int:
    result = infer_session_result(change)
    outcome = _handle_terminal_outcome(change, result, coord)
    if result is RunResult.PAUSED and coord.should_pause:
        return 130
    return outcome


def _project_root() -> Path:
    return find_project_root()


def _control_root() -> Path:
    return worktree.find_control_root(Path.cwd())


def _preflight_imp_init(*, control_root: Path, project_root: Path) -> str | None:
    """Return a clean 'run imp init first' message when no provider is
    env-available (e.g. a never-init'd machine with empty user `providers`),
    else None. Relocates+dresses the failure that per-step resolution would
    otherwise raise as a raw ProviderUnavailable deep in a step (spec §4)."""
    from imp.providers.effective import load_effective_config
    from imp.providers.resolve import UnknownProvider, _first_available_provider
    try:
        config = load_effective_config(
            project_root=project_root, control_root=control_root,
        ).config
        no_provider = _first_available_provider(config) is None
    except (LoadError, LoadErrors, UnknownProvider):
        # A malformed config (bad YAML, or a `providers:` entry that names an
        # unregistered provider — _first_available_provider raises UnknownProvider,
        # resolve.py:124-136) is reported downstream with full context. Don't dress
        # it as "run imp init first" — that would mislead. Let resolution surface it.
        return None
    if no_provider:
        return "no provider available — run `imp init` first"
    return None


def _verify_command_nudge(
    *, project_root: Path, control_root: Path, non_interactive: bool,
    input_fn: Callable[[str], str], output_fn: Callable[[str], None],
    err_fn: Callable[[str], None],
    detect_fn: Callable[[Path], str | None] | None = None,
) -> None:
    """`imp new` backstop: nudge when verify_command is absent and a command is detected."""
    from imp.cli.verify_detect import (
        detect_verify_command,
        format_verify_command_yaml,
        resolve_verify_choice,
    )
    from imp.providers.effective import load_effective_config
    detect = detect_fn or detect_verify_command
    try:
        cfg = load_effective_config(
            project_root=project_root, control_root=control_root).config
    except (LoadError, LoadErrors):
        return
    if cfg.verify_command is not None:
        return
    suggested = detect(project_root)
    if suggested is None:
        return
    project_file = project_root / "imp" / "project.yaml"
    if non_interactive:
        err_fn(
            f"imp: no verify_command configured; detected `{suggested}`. Set "
            f"verify_command in {project_file} to gate regressions in-loop.")
        return
    chosen = resolve_verify_choice(suggested, input_fn, output_fn)
    existing = project_file.read_text()
    with project_file.open("a") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        f.write(format_verify_command_yaml(chosen))


def _apply_theme() -> None:
    """Pick the active color theme once, before any command renders. `IMP_THEME`
    overrides everything; otherwise the value detected and persisted at `imp init`
    (config.theme); otherwise the universal `ansi`. Best-effort — a missing or
    unreadable config must never fail a command over theming, it just stays ansi."""
    override = os.environ.get("IMP_THEME", "").strip().lower()
    if override in pal.THEMES:
        pal.set_theme(override)
        return
    from imp.providers.effective import load_effective_config
    try:
        root = _project_root()
        theme = load_effective_config(project_root=root, control_root=root).config.theme
        pal.set_theme(theme)
    except Exception:
        pass


def _cmd_init(args: argparse.Namespace) -> int:
    project = _project_root()
    try:
        run_init(project, input_fn=input, output_fn=print)
    except GitSetupDeclined as e:
        print(f"imp: {e}", file=sys.stderr)
        return 2
    except FileExistsError as e:
        print(f"imp: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nimp: init cancelled", file=sys.stderr)
        return 130
    return 0


def _run_header_line(change_id: str, title: str, marker: str,
                     *, color: bool) -> str:
    stripped = title.strip()
    tail = f" · {stripped} · {marker}" if stripped else f" · {marker}"
    return (ansi(change_id, *pal.NODE_COLORS["change_id"], enabled=color)
            + ansi(tail, "dim", enabled=color))


def _print_run_header(change_id: str, title: str, marker: str,
                      *, color: bool) -> None:
    print(_run_header_line(change_id, title, marker, color=color))
    print()


def _warn_if_tui_unavailable(args: argparse.Namespace, stream: Any) -> None:
    if getattr(args, "tui", False) and not getattr(stream, "isatty", lambda: False)():
        print("imp: --tui needs an interactive terminal; streaming instead.",
              file=sys.stderr)


def _tui_requested(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "tui", False)) and sys.stdout.isatty()


def _is_benign_interrupt_teardown(exc: BaseException | None) -> bool:
    """True when exc is interrupt-teardown noise that must not surface in --tui (modal wedge)."""
    import asyncio
    if isinstance(exc, asyncio.InvalidStateError):
        return True
    if exc is None:
        return False
    name = type(exc).__name__
    if name == "CLIConnectionError" and "terminated process" in str(exc):
        return True
    if name == "ProcessError" and getattr(exc, "exit_code", None) == 143:
        return True
    return "(exit code: 143)" in str(exc)


@contextlib.contextmanager
def _benign_interrupt_loop_handler(coord: Any) -> Iterator[None]:
    import asyncio
    loop = asyncio.get_running_loop()
    prev = loop.get_exception_handler()

    def handler(lp: Any, context: dict[str, Any]) -> None:
        if coord.should_pause or _is_benign_interrupt_teardown(
                context.get("exception")):
            return
        if prev is not None:
            prev(lp, context)
        else:
            lp.default_exception_handler(context)

    loop.set_exception_handler(handler)
    try:
        yield
    finally:
        loop.set_exception_handler(prev)


async def _run_tui_session(
        change: Change, *, seed_opening: str | None = None,
        resume: bool = False,
        opening_intent: str | None = None,
        leave_mode: str | None = None) -> int:
    from imp.cli.signals import InterruptCoordinator

    coord = InterruptCoordinator()
    app = TuiApp(coord=coord, color=should_use_color(sys.stdout), width=_term_width())
    attach = build_session_attach(
        change, seed_opening=seed_opening, resume=resume,
        opening_intent=opening_intent)
    leave_state: dict[str, str] = {}

    async def _solicit(_prompt: str) -> str:
        return await app.solicit(change)

    with _benign_interrupt_loop_handler(coord):
        async with anyio.create_task_group() as tg:
            tg.start_soon(coord.run_signal_receiver)

            async def _run() -> None:
                try:
                    with contextlib.suppress(TimeoutError):
                        await run_terminal_client(
                            attach=attach, driver=app, coord=coord, solicit=_solicit,
                            change_id=change.id, leave_state=leave_state)
                finally:
                    app.exit()

            tg.start_soon(_run)
            await app.run_async()
            tg.cancel_scope.cancel()

    app.print_collapsed_transcript(sys.stdout)
    disposition_rc = await _maybe_apply_leave_disposition(change, leave_mode, leave_state)
    if disposition_rc is not None:
        return disposition_rc
    return _session_exit_code(change, coord)


def _record_idea(args: argparse.Namespace, control: Path) -> int:
    title = args.idea
    if not title.strip():
        print("imp: idea title must be non-empty", file=sys.stderr)
        return 2
    description = getattr(args, "description", None) or ""
    seed = backlog.seed_opening({"title": title, "description": description})
    base = worktree.current_branch(control)
    change_id = new_change_id()
    try:
        Change.create_draft(
            control, title=title, change_id=change_id, base_branch=base,
            depends_on=list(getattr(args, "depends_on", None) or []),
            workflow=getattr(args, "workflow", None),
            origin="idea", seed_opening=seed)
    except (LoadError, LoadErrors) as e:
        print(f"imp: {e}", file=sys.stderr)
        return 2
    print(f"imp: recorded backlog Change {change_id} "
          f"(start it with `imp resume {change_id}`)")
    return 0


def _select_headless(
    args: argparse.Namespace, *, stdin: Any = None, stdout: Any = None,
) -> tuple[bool, bool]:
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    non_interactive = bool(getattr(args, "non_interactive", False))
    if getattr(args, "tui", False):
        return False, False  # explicit --tui: let _tui_requested decide availability
    interactive = not non_interactive and stdin.isatty() and stdout.isatty()
    headless = non_interactive or not interactive
    return headless, headless and not non_interactive


async def _run_fresh_session(
    change: Change, *, args: argparse.Namespace, seed_opening: str | None,
) -> int:
    """Run a freshly-created change (new / playbook / materialized backlog) through
    the per-change session daemon, dispatching on headless / --tui / interactive."""
    from imp.cli.signals import InterruptCoordinator

    _warn_if_tui_unavailable(args, sys.stdout)
    _print_run_header(
        change.id, change.title, "new", color=should_use_color(sys.stdout))
    headless, auto_headless = _select_headless(args)
    if headless:
        if auto_headless:
            print(f"imp: no tty detected — running headless; resume with "
                  f"`imp resume {change.id}`", file=sys.stderr)
        status = LiveStatus(sys.stdout)
        driver = LiveDriver(stream=sys.stdout,
                            color=should_use_color(sys.stdout),
                            width=_term_width(), status=status)
        coord = InterruptCoordinator()
        solicit = make_headless_solicit(getattr(args, "message", None), coord)
        return await run_session_client(
            change, driver=driver, coord=coord, solicit=solicit,
            seed_opening=seed_opening, status=status)
    if _tui_requested(args):
        return await _run_tui_session(change, seed_opening=seed_opening)
    status = LiveStatus(sys.stdout)
    driver = LiveDriver(stream=sys.stdout,
                        color=should_use_color(sys.stdout),
                        width=_term_width(), status=status)
    coord = InterruptCoordinator()
    solicit = make_interactive_solicit(
        change, status=status, driver=driver, coord=coord)
    return await run_session_client(
        change, driver=driver, coord=coord, solicit=solicit,
        seed_opening=seed_opening, status=status)


async def _async_cmd_new(args: argparse.Namespace, *, leave_mode: str = "new") -> int:
    install_process_group()
    from imp.cli.signals import InterruptCoordinator

    control = _control_root()
    worktree.check_preconditions(control)
    worktree.ensure_imp_ignored(control)
    base = worktree.current_branch(control)

    if getattr(args, "description", None) and getattr(args, "idea", None) is None:
        print("imp: --description requires --idea", file=sys.stderr)
        return 2

    wf_arg = getattr(args, "workflow", None)
    if wf_arg is not None and not validate_workflow_name(wf_arg):
        print(f"imp: invalid workflow name {wf_arg!r}", file=sys.stderr)
        return 2

    if getattr(args, "idea", None) is not None:
        if getattr(args, "title", None):
            print("imp: --idea and --title cannot be combined "
                  "(--idea is the title)", file=sys.stderr)
            return 2
        return _record_idea(args, control)

    try:
        playbook_name = getattr(args, "playbook", None)
        if playbook_name and getattr(args, "spec_file", None):
            raise SeedError("--playbook and --spec-file are mutually exclusive")
        if getattr(args, "findings_only", False) and not playbook_name:
            raise SeedError("--findings-only requires --playbook")
        seed = resolve_spec_seed(args)
        if playbook_name:
            seed = SeedResolution(
                seed.title,
                resolve_playbook_seed(
                    control, playbook_name, getattr(args, "opening", None),
                    findings_only=getattr(args, "findings_only", False)),
                opening_intent=None)
    except (SeedError, ValueError) as e:
        print(f"imp: {e}", file=sys.stderr)
        return 2

    block = _preflight_imp_init(control_root=control, project_root=control)
    if block is not None:
        print(f"imp: {block}", file=sys.stderr)
        return 2

    non_interactive = bool(getattr(args, "non_interactive", False))
    _verify_command_nudge(
        project_root=control, control_root=control,
        non_interactive=non_interactive,
        input_fn=input, output_fn=print,
        err_fn=lambda m: print(m, file=sys.stderr))

    interactive = _is_interactive(non_interactive=non_interactive)
    opening_text = seed.seed_text
    opening_intent = seed.opening_intent
    banner_shown = _print_splash_banner(interactive=interactive)
    if _should_prompt_opening(seed_text=seed.seed_text, interactive=interactive):
        if banner_shown:
            print()
        color = should_use_color(sys.stdout)
        prompt = Prompt(editing_mode=_editing_mode(control), color=color)
        try:
            reply = (await prompt.ask_async(width=_term_width(),
                                            hint=OPENING_HINT)).strip()
        except (EOFError, KeyboardInterrupt):
            print("imp: cancelled", file=sys.stderr)
            return 130
        # An empty reply falls back to the unseeded path (the spec-writer opens
        # by asking); a real answer becomes the opening intent.
        opening_text = _normalize_opening_reply(reply)
        opening_intent = opening_text

    change_id = new_change_id()
    # No auto-name: an unnamed Change has an empty title. The agent
    # names it via rename_change once intent is clear, or the user passes --title.
    title = seed.title if seed.title is not None else ""
    try:
        change = Change.create_draft(control, title=title, change_id=change_id,
                                     base_branch=base, workflow=wf_arg,
                                     origin="draft")
    except (LoadError, LoadErrors) as e:
        print(f"imp: {e}", file=sys.stderr)
        return 2

    _warn_if_tui_unavailable(args, sys.stdout)
    headless, auto_headless = _select_headless(args)
    if headless:
        if auto_headless:
            print(f"imp: no tty detected — running headless; resume with "
                  f"`imp resume {change.id}`", file=sys.stderr)
        _print_run_header(
            change.id, change.title, leave_mode, color=should_use_color(sys.stdout))
        status = LiveStatus(sys.stdout)
        driver = LiveDriver(stream=sys.stdout,
                            color=should_use_color(sys.stdout),
                            width=_term_width(), status=status)
        coord = InterruptCoordinator()
        solicit = make_headless_solicit(getattr(args, "message", None), coord)
        return await run_session_client(
            change, driver=driver, coord=coord, solicit=solicit,
            seed_opening=opening_text, status=status,
            opening_intent=opening_intent)

    if _tui_requested(args):
        _print_run_header(
            change.id, change.title, leave_mode, color=should_use_color(sys.stdout))
        return await _run_tui_session(change, seed_opening=opening_text,
                                      opening_intent=opening_intent,
                                      leave_mode=leave_mode)

    _print_run_header(
        change.id, change.title, leave_mode, color=should_use_color(sys.stdout))
    status = LiveStatus(sys.stdout)
    driver = LiveDriver(stream=sys.stdout,
                        color=should_use_color(sys.stdout),
                        width=_term_width(), status=status)
    coord = InterruptCoordinator()
    solicit = make_interactive_solicit(
        change, status=status, driver=driver, coord=coord)
    return await run_session_client(
        change, driver=driver, coord=coord, solicit=solicit,
        seed_opening=opening_text, status=status,
        opening_intent=opening_intent, leave_mode=leave_mode)


def _cmd_new(args: argparse.Namespace) -> int:
    return anyio.run(functools.partial(_async_cmd_new, leave_mode="new"), args)


def _cmd_chat(args: argparse.Namespace) -> int:
    return anyio.run(functools.partial(_async_cmd_new, leave_mode="chat"), args)


async def _async_cmd_run(args: argparse.Namespace) -> int:
    control = _control_root()
    name = args.name
    if name is None:
        picked = await _pick_playbook(
            control,
            non_interactive=args.non_interactive)
        if isinstance(picked, int):
            return picked
        name = picked
    try:
        if playbooks.find_playbook(control, name) is None:
            available = ", ".join(p.name for p in playbooks.list_playbooks(control))
            print(f"imp: unknown playbook {name!r}; available: {available}",
                  file=sys.stderr)
            return 2
    except ValueError as e:
        print(f"imp: {e}", file=sys.stderr)
        return 2
    args.playbook = name
    args.opening = args.playbook_args
    args.spec_file = None
    return await _async_cmd_new(args)


def _cmd_run(args: argparse.Namespace) -> int:
    return anyio.run(_async_cmd_run, args)


def _resume_seed(change: Change) -> str | None:
    """Return the seed_opening for an un-materialized home that has no prior conversation.

    Returns None for a materialized change, a home without a seed, or any home
    that already holds a conversation (those resume into their existing event-log)."""
    if change.materialized or not change.seed_opening:
        return None
    events = _load_events_for_id(change.control_root, change.id) or []
    if _first_user_message(events) is not None:
        return None
    return change.seed_opening


def _warn_if_blocked(change: Change) -> None:
    try:
        deps = json.loads((change.change_dir / "change.json").read_bytes()).get("depends_on", [])
    except (FileNotFoundError, json.JSONDecodeError):
        return
    ready, unmerged = backlog.readiness(change.control_root, deps)
    if not ready:
        print(f"imp: backlog Change {change.id} is blocked on unmerged dep(s): "
              f"{', '.join(unmerged)}; starting anyway.", file=sys.stderr)


async def _async_cmd_resume(args: argparse.Namespace) -> int:
    install_process_group()
    from imp.cli.signals import InterruptCoordinator

    control = _control_root()
    if args.token is None:
        target = await _pick_resume_target(
            control, non_interactive=bool(getattr(args, "non_interactive", False)))
        if isinstance(target, int):
            return target
        change_id = target
    else:
        try:
            change_id = _resolve_id(control, args.token)
        except SystemExit:
            return 2
    loaded = _load_or_rehydrate(control, change_id)
    if isinstance(loaded, int):
        return loaded
    change = loaded
    seed = _resume_seed(change)
    if seed is not None:
        _warn_if_blocked(change)
        return await _run_fresh_session(change, args=args, seed_opening=seed)
    state = State.load(change.state_dir)
    # `running` is allowed through: the status may be stale if a previous run
    # crashed without updating it. The daemon resolves the ambiguity at connect time.
    if state.status not in RESUMABLE and state.status != "running":
        print(
            f"imp: Change {change.id} status is {state.status!r}; "
            "nothing to resume",
            file=sys.stderr,
        )
        return 2

    headless, auto_headless = _select_headless(args)
    if headless:
        if auto_headless:
            print(f"imp: no tty detected — running headless; resume with "
                  f"`imp resume {change.id}`", file=sys.stderr)
        print(_run_header_line(
            change.id, change.title, "resuming", color=should_use_color(sys.stdout)))
        status = LiveStatus(sys.stdout)
        driver = LiveDriver(stream=sys.stdout,
                            color=should_use_color(sys.stdout),
                            width=_term_width(), status=status,
                            suppress_replay=True)
        coord = InterruptCoordinator()
        solicit = make_headless_solicit(getattr(args, "message", None), coord)
        return await run_session_client(
            change, driver=driver, coord=coord, solicit=solicit,
            resume=True, status=status)
    if _tui_requested(args):
        print(_run_header_line(
            change.id, change.title, "resuming", color=should_use_color(sys.stdout)))
        return await _run_tui_session(change, resume=True)

    # Interactive resume: a thin session-client; the daemon owns the worktree +
    # lock and runs resume prep + the agent loop.
    _warn_if_tui_unavailable(args, sys.stdout)
    _print_splash_banner(interactive=_is_interactive(
        non_interactive=bool(getattr(args, "non_interactive", False))))
    print(_run_header_line(
        change.id, change.title, "resuming", color=should_use_color(sys.stdout)))
    status = LiveStatus(sys.stdout)
    driver = LiveDriver(stream=sys.stdout,
                        color=should_use_color(sys.stdout),
                        width=_term_width(), status=status,
                        suppress_replay=True)
    coord = InterruptCoordinator()
    solicit = make_interactive_solicit(
        change, status=status, driver=driver, coord=coord)
    return await run_session_client(
        change, driver=driver, coord=coord, solicit=solicit,
        resume=True, status=status)


def _cmd_resume(args: argparse.Namespace) -> int:
    return anyio.run(_async_cmd_resume, args)


def _editing_mode(control_root: Path) -> str:
    """Read editing_mode from the merged config; default to emacs if config is
    absent or invalid (run_change reports a bad config with full context — don't
    double-fail here just to pick a prompt mode)."""
    from imp.providers.effective import load_effective_config
    try:
        return load_effective_config(
            project_root=control_root, control_root=control_root,
        ).config.editing_mode
    except (LoadError, LoadErrors):
        return "emacs"


def _term_width() -> int:
    # shutil.get_terminal_size honors $COLUMNS and substitutes the fallback when
    # the OS reports columns <= 0 (e.g. a winsize-0 PTY), unlike
    # os.get_terminal_size which returns a literal 0 and floors wrapping to 8
    # chars (issue 2). Defense-in-depth with the dogfood PTY winsize fix.
    return shutil.get_terminal_size((80, 24)).columns


def _list_change_homes(control: Path) -> list[str]:
    """Ids of changes with a permanent root home under .imp/changes/."""
    root = control / ".imp" / "changes"
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir()
                  if p.is_dir() and not p.name.startswith(".")
                  and (p / "change.json").exists())


def _change_home(control: Path, change_id: str) -> Path:
    return control / ".imp" / "changes" / change_id


def _iter_change_ids(control: Path) -> list[str]:
    """All known Change ids: permanent homes first, then archived ids not live."""
    live = _list_change_homes(control)
    seen = set(live)
    out = list(live)
    for cid in archive_read.archived_ids(control):
        if cid not in seen:
            out.append(cid)
            seen.add(cid)
    return out


def _title_for_id(control: Path, change_id: str) -> str | None:
    """Read a Change's current title (worktree home first, archive second)."""
    cdir = _change_home(control, change_id)
    cj = cdir / "change.json"
    if cj.exists():
        try:
            return str(json.loads(cj.read_bytes())["title"])
        except (json.JSONDecodeError, KeyError):
            return None
    raw = archive_read.read_change_json(control, change_id)
    if raw is None:
        return None
    try:
        return str(json.loads(raw)["title"])
    except (json.JSONDecodeError, KeyError):
        return None


def _resolve_id(control: Path, token: str) -> str:
    """Resolve a user-typed token to a Change id (spec AC 7): it must be an
    unambiguous substring of exactly one change's id. Multiple matches exit
    non-zero listing candidates; none errors."""
    if not token.strip():
        print(f"imp: no Change matches {token!r}", file=sys.stderr)
        raise SystemExit(2)
    matches = [cid for cid in _iter_change_ids(control) if token in cid]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        _exit_ambiguous(token, matches, control)
    print(f"imp: no Change matches {token!r}", file=sys.stderr)
    raise SystemExit(2)


def _exit_ambiguous(token: str, candidates: list[str], control: Path) -> NoReturn:
    print(f"imp: {token!r} is ambiguous; candidates:", file=sys.stderr)
    color = should_use_color(sys.stderr)
    now = datetime.now(UTC)
    print(change_row.header_line(color=color), file=sys.stderr)
    for cid in sorted(candidates):
        row = _row_for_id(control, cid)
        if row is None:
            print(f"  {cid}", file=sys.stderr)
            continue
        print(change_row.format_change_line(
            change_id=row[0], title=row[4], status=row[1], step=row[2],
            step_label=row[6], last_iso=row[3], now=now, color=color,
            name_width=change_row.name_budget()), file=sys.stderr)
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


def _show_artifact(control: Path, change_id: str, rel: str) -> bytes | None:
    live = _change_home(control, change_id) / "artifacts" / rel
    if live.is_file():
        return live.read_bytes()
    return archive_read.read_artifact(control, change_id, rel)


def _show_newest_plan_rel(control: Path, change_id: str) -> str | None:
    """plans/<id>-plan-N.md for the highest N: live worktree first, then the
    retained wip ref. Returns None when no plan exists anywhere."""
    live_dir = _change_home(control, change_id) / "artifacts" / "plans"
    if live_dir.is_dir():
        pick = archive_read.pick_newest_plan(
            change_id, [p.name for p in live_dir.iterdir() if p.is_file()])
        if pick:
            return f"plans/{pick}"
    pick = archive_read.newest_plan_name(control, change_id)
    if pick:
        return f"plans/{pick}"
    return None


def _show_runtime(control: Path, change_id: str, name: str) -> bytes | None:
    change = _change_home(control, change_id)
    live = change / "state" / "state.json" if name == "state.json" else change / name
    if live.is_file():
        return live.read_bytes()
    if name == "state.json":
        return archive_read.read_state_json(control, change_id)
    return archive_read.read_events(control, change_id)


_REVIEW_STEPS = frozenset({
    "spec-reviewer", "plan-reviewer", "plan-code-reviewer", "deferred-sweep",
    "plan-cycle-check", "finalize",
})


def _user_message_text(payload: dict[str, Any]) -> str:
    return "".join(
        b.get("text", "") for b in (payload.get("content") or [])
        if isinstance(b, dict) and b.get("type") == "text")


def _first_user_message(events: list[dict[str, Any]]) -> str | None:
    for ev in events:
        if ev.get("type") == "user_message":
            text = _user_message_text(ev.get("payload") or {}).strip()
            if text:
                return text
    return None


def _resolve_intent(events: list[dict[str, Any]]) -> str | None:
    for ev in events:
        if ev.get("type") == "opening_intent":
            text = ((ev.get("payload") or {}).get("text") or "").strip()
            if text:
                return text
    return _first_user_message(events)


def _latest_phase(events: list[dict[str, Any]]) -> str | None:
    phase = None
    for ev in events:
        if ev.get("type") == "step_started":
            phase = (ev.get("payload") or {}).get("phase") or phase
    return phase


def _change_meta(control: Path, change_id: str) -> dict[str, Any] | None:
    cdir = _change_home(control, change_id)
    raw: bytes | None
    if (cdir / "change.json").exists():
        raw = (cdir / "change.json").read_bytes()
    else:
        raw = archive_read.read_change_json(control, change_id)
    if raw is None:
        return None
    try:
        result: dict[str, Any] = json.loads(raw)
        return result
    except json.JSONDecodeError:
        return None


def _unstarted_backlog_meta(control: Path, change_id: str) -> dict[str, Any] | None:
    """change.json of a LIVE un-materialized, never-conversed Backlog home (idea or
    followup) — sourced from the worktree home only, never the archive: an abandoned
    home leaves an archived materialized:false change.json that would otherwise render
    as a queued `status: backlog` brief instead of a History item. None for any
    archived-only, materialized, or already-conversed id."""
    cdir = _change_home(control, change_id)
    cj = cdir / "change.json"
    if not cj.exists():
        return None
    try:
        meta: dict[str, Any] = json.loads(cj.read_bytes())
    except json.JSONDecodeError:
        return None
    if meta.get("materialized", True):
        return None
    try:
        change = Change.load(control, change_id)
    except (LoadError, LoadErrors):
        return None
    if _first_user_message(list(change.event_log().read())) is not None:
        return None
    return meta


def _backlog_card(meta: dict[str, Any], control: Path, *, now: datetime) -> list[str]:
    ready, unmerged = backlog.readiness(control, meta.get("depends_on", []))
    readiness = "ready" if ready else f"blocked on {', '.join(unmerged)}"
    created = meta.get("created_at")
    lines = [
        f"id:         {meta['id']}",
        f"title:      {meta.get('title') or '(unnamed)'}",
        "status:     backlog",
        f"provenance: {meta.get('origin', 'draft')}",
        f"readiness:  {readiness}",
        f"created:    {created or '-'} ({change_row.relative_age(created, now=now)})",
    ]
    if meta.get("depends_on"):
        lines.append(f"depends_on: {', '.join(meta['depends_on'])}")
    body = meta.get("seed_opening") or ""
    title = meta.get("title") or ""
    if title and (body == title or body.startswith(title + "\n")):
        body = body[len(title):].lstrip("\n")
    if body:
        lines.append("")
        lines.append(body)
    return lines


def _change_card(control: Path, change_id: str, *, now: datetime) -> list[str]:
    row = _row_for_id(control, change_id)
    if row is None:
        return []
    _id, status, step, last, title, created, _step_label = row
    events = _load_events_for_id(control, change_id) or []
    lines = [
        f"id:            {_id}",
        f"title:         {title.strip() or '(unnamed)'}",
        f"status:        {status}",
    ]
    if status in _INFLIGHT and step not in ("", "-"):
        phase = _latest_phase(events)
        lines.append(f"step:          {step}" + (f" ({phase})" if phase else ""))
    lines.append(
        f"created:       {created or '-'} ({change_row.relative_age(created, now=now)})")
    lines.append(f"last activity: {change_row.relative_age(last, now=now)}")
    meta = _change_meta(control, change_id) or {}
    if meta.get("base_branch"):
        lines.append(f"base_branch:   {meta['base_branch']}")
    if meta.get("depends_on"):
        lines.append(f"depends_on:    {', '.join(meta['depends_on'])}")
    opening = _first_user_message(events)
    if opening:
        lines.append("")
        lines.append(opening)
    return lines


def _detail_card(control: Path, change_id: str, *, now: datetime) -> list[str]:
    meta = _unstarted_backlog_meta(control, change_id)
    if meta is not None:
        return _backlog_card(meta, control, now=now)
    return _change_card(control, change_id, now=now)


def _cmd_show(args: argparse.Namespace) -> int:
    control = _control_root()
    try:
        change_id = _resolve_id(control, args.token)
    except SystemExit:
        return 2
    resource = args.resource
    meta = _unstarted_backlog_meta(control, change_id)
    if meta is not None:
        if resource is None:
            for line in _backlog_card(meta, control, now=datetime.now(UTC)):
                print(line)
            return 0
        if resource == "status":
            print("backlog")
            return 0
        print(f"imp: {change_id} is in the backlog (not started); "
              f"no {resource} yet", file=sys.stderr)
        return 2
    if resource is None:
        for line in _detail_card(control, change_id, now=datetime.now(UTC)):
            print(line)
        return 0
    if resource == "spec":
        data = _show_artifact(control, change_id, f"spec/{change_id}.md")
    elif resource == "plan":
        rel = _show_newest_plan_rel(control, change_id)
        data = _show_artifact(control, change_id, rel) if rel else None
    elif resource == "events":
        data = _show_runtime(control, change_id, "events.jsonl")
    elif resource == "status":
        raw = _show_runtime(control, change_id, "state.json")
        if raw is None:
            print(f"imp: no state for {change_id!r}", file=sys.stderr)
            return 2
        print(json.loads(raw).get("status", "unknown"))
        return 0
    elif resource == "review":
        raw = _show_runtime(control, change_id, "events.jsonl")
        if raw is None:
            print(f"imp: no events for {change_id!r}", file=sys.stderr)
            return 2
        for ev in _parse_jsonl(raw):
            payload = ev.get("payload") or {}
            if ev.get("type") == "step_started" and \
                    payload.get("step") in _REVIEW_STEPS:
                print(f"{payload.get('phase', '?')}/{payload.get('step')}")
        return 0
    if data is None:
        print(f"imp: no {resource} for {change_id!r}", file=sys.stderr)
        return 2
    sys.stdout.write(data.decode("utf-8", errors="replace"))
    return 0


def _load_events_for_id(control: Path, change_id: str) -> list[dict[str, Any]] | None:
    """Worktree first, archive second. Returns None if the id is in neither.

    Worktree-first is necessary, not merely preferred: an id lives in exactly
    one place (the archive snapshot is written once, concurrent with worktree
    removal), so the only overlap is a crash mid-sequence where worktree-first
    harmlessly returns identical content."""
    try:
        change = Change.load(control, change_id)
    except LoadError:
        change = None
    if change is not None:
        return list(change.event_log().read())
    raw = archive_read.read_events(control, change_id)
    if raw is None:
        return None
    return _parse_jsonl(raw)


def _resolve_default_id(control: Path) -> str | None:
    """No-arg default: most-recent change home by its events.jsonl mtime, else
    most-recent archived id (by last-event timestamp), else None."""
    homes = _list_change_homes(control)
    if homes:
        return max(homes, key=lambda s: (_change_home(control, s)
                                         / "events.jsonl").stat().st_mtime)
    archived = archive_read.archived_ids(control)
    if not archived:
        return None
    return max(archived, key=lambda s: _journal_last_iso(control, s) or "")


_NO_CHANGES_MESSAGE = "No changes."


def _log_picker_empty() -> int:
    print("No changes.")
    return 2


async def _pick_log_target(control: Path) -> str | int:
    rows = _sort_rows_newest_first(
        _status_rows(control) + _backlog_rows(control) + _completed_rows(control))
    default_id = _resolve_default_id(control)
    selected_index = next(
        (i for i, r in enumerate(rows) if r[0] == default_id), 0)

    return await _pick_change_target(
        rows=rows,
        header="Show log for which change?  (↑/↓ to move · enter to select · q to cancel)",
        on_empty=_log_picker_empty,
        selected_index=selected_index,
    )


def _page_or_print(lines: Iterable[str], *, no_pager: bool) -> None:
    # Pages lines through $PAGER on a tty; falls back to print() otherwise.
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

    control = _control_root()
    change_id: str | None
    if args.token is not None:
        try:
            change_id = _resolve_id(control, args.token)
        except SystemExit:
            return 2
    elif _interactive():
        target = anyio.run(_pick_log_target, control)
        if isinstance(target, int):
            return target
        change_id = target
    else:
        change_id = _resolve_default_id(control)
    if change_id is None:
        print(_NO_CHANGES_MESSAGE)
        return 0
    meta = _unstarted_backlog_meta(control, change_id)
    if meta is not None:
        lines: Iterable[str] = _backlog_card(meta, control, now=datetime.now(UTC))
        _page_or_print(lines, no_pager=args.no_pager)
        return 0
    events = _load_events_for_id(control, change_id)
    if events is None:
        print(f"imp: no such Change {change_id!r}", file=sys.stderr)
        return 2

    width = _term_width()
    color = should_use_color(sys.stdout)

    if args.summary:
        from imp.cli.render_summary import (
            SummaryHeader,
            build_summary,
            render_summary,
            summary_total,
        )
        row = _row_for_id(control, change_id)
        status = row[1] if row is not None else "?"
        workflow = parse_default_workflow(control / "imp" / "project.yaml")
        header = SummaryHeader(workflow=workflow, change_id=change_id,
                               status=status, total_seconds=summary_total(events))
        lines = render_summary(build_summary(events), header=header,
                               color=color, width=width)
        _page_or_print(lines, no_pager=args.no_pager)
        return 0

    tree = narrative.build_tree(events)
    lines = render_tree(tree, full=args.full, color=color, width=width)
    _page_or_print(lines, no_pager=args.no_pager)
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


def _iso_from_line(line: str) -> str | None:
    if not line.strip():
        return None
    try:
        return str(json.loads(line)["timestamp"])
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def _last_event_iso(events_path: Path) -> str | None:
    try:
        line = _read_last_line(events_path)
    except FileNotFoundError:
        return None
    if line is None:
        return None
    return _iso_from_line(line.decode("utf-8", errors="replace"))


def _journal_last_iso(control: Path, change_id: str) -> str | None:
    return archive_read.last_event_iso(control, change_id)


def _journal_status(control: Path, change_id: str) -> str | None:
    return archive_read.status_of(control, change_id)


def _resume_gone(control: Path, change_id: str) -> int:
    status = _journal_status(control, change_id)
    title = _title_for_id(control, change_id) or change_id
    if status == "abandoned":
        print(f"{title} was abandoned; nothing to resume.")
        return 0
    if status is not None:
        print(f"{title} is already completed; nothing to resume.")
        return 0
    print(f"imp: no Change matches {change_id!r}", file=sys.stderr)
    return 2


def _rehydrate_resumable(control: Path, change_id: str) -> Change | None:
    from imp.engine.archive import record, records_root
    rec = record.read_record(records_root(control), change_id)
    if rec is None or not rec.get("resumable"):
        return None
    try:
        archive_read.rehydrate_change(control, change_id)
        return Change.load(control, change_id)
    except (FileExistsError, LoadError, LoadErrors):
        return None


def _load_or_rehydrate(control: Path, change_id: str) -> Change | int:
    try:
        return Change.load(control, change_id)
    except LayoutMismatch as e:
        print(f"imp: {e}", file=sys.stderr)
        return 2
    except (LoadError, LoadErrors):
        change = _rehydrate_resumable(control, change_id)
        if change is None:
            return _resume_gone(control, change_id)
        return change


# Spec §10: in-flight statuses; completed Changes live in the archive.
_INFLIGHT = ("running", "paused", "errored", "merge_blocked", "draft")
RESUMABLE = ("paused", "errored", "merge_blocked", "draft", "saved")


def _pid_alive(pid: int) -> bool:
    """Signal-0 liveness probe. ESRCH (no such process) → dead; EPERM (exists,
    not ours) → alive. A live run's pid is never falsely reported dead, so this
    never lets two processes into one worktree — the safe direction."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _effective_status(status: str, change_dir: Path) -> str:
    """Re-derive a `running` status against its ProjectLock holder. A run holds
    `change_dir/lock` (pid inside) for its whole life; a hard kill / crash has the
    OS drop the flock but leaves the stale pid file and a `running` state.json. So
    a `running` Change whose lock pid is dead has CRASHED — surface it as
    `errored` (in RESUMABLE) so `imp ls` shows it and `imp resume` offers it.

    Only `running` is re-derived; every other status is authoritative. A MISSING
    lock file is left as `running` (the microsecond between Change.create_draft and the
    run acquiring the lock) — a real crash leaves the pid file behind."""
    if status != "running":
        return status
    try:
        pid = int((change_dir / "lock").read_text().strip())
    except (FileNotFoundError, ValueError):
        return status
    return status if _pid_alive(pid) else "errored"


# Row = (id, status, step, last_activity, title, created_at, step_label).
# created_at is the trailing sort key only; step_label feeds the merged STATUS
# cell's ` (label)` suffix (see change_row.format_change_line). The step slot
# stays the RAW step name so imp show keeps printing it.
_Row = tuple[str, str, str, str, str, str, str | None]


def _status_row(change_id: str, state_json: bytes, change_json: bytes,
                last_activity: str) -> _Row | None:
    """Pure (id, status, step, last_activity, title, created_at) extractor, fed
    by either worktree file bytes or archive bytes. The archived state.json and
    change.json are schema-identical to the in-flight ones (both written by
    State.save / Change.create), so the two sections can't silently drift.
    Returns None on malformed/incomplete JSON so a single corrupt entry can't
    crash the whole `imp ls` listing (callers skip None)."""
    try:
        st = json.loads(state_json)
        meta = json.loads(change_json)
        return (change_id, st["status"], st.get("step") or "-", last_activity,
                meta["title"], meta["created_at"], st.get("step_label"))
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def _draft_label(status: str, change_json: bytes) -> str:
    """Returns "draft" for un-materialized paused changes; otherwise status."""
    if status != "paused":
        return status
    try:
        if json.loads(change_json).get("materialized", True) is False:
            return "draft"
    except (json.JSONDecodeError, TypeError, AttributeError):
        pass
    return status


def _row_for_id(control: Path, change_id: str) -> _Row | None:
    cdir = _change_home(control, change_id)
    # archive_read returns bytes | None; the live change-home branch always finds
    # the files. Annotate so the None-guard below stays reachable (not dead code).
    state_json: bytes | None
    change_json: bytes | None
    if (cdir / "change.json").exists():
        state_json = (cdir / "state" / "state.json").read_bytes()
        change_json = (cdir / "change.json").read_bytes()
        last = _last_event_iso(cdir / "events.jsonl") or ""
    else:
        state_json = archive_read.read_state_json(control, change_id)
        change_json = archive_read.read_change_json(control, change_id)
        if state_json is None or change_json is None:
            return None
        last = _journal_last_iso(control, change_id) or ""
    row = _status_row(change_id, state_json, change_json, last)
    if row is None:
        return None
    # A live worktree may hold a crashed `running` run — re-derive against its
    # lock (journaled rows are terminal, so the check is a no-op there).
    status = _draft_label(_effective_status(row[1], cdir), change_json)
    return (row[0], status, *row[2:])


def _status_rows(control: Path) -> list[_Row]:
    rows: list[_Row] = []
    for change_id in _list_change_homes(control):
        cdir = _change_home(control, change_id)
        try:
            state_json = (cdir / "state" / "state.json").read_bytes()
            change_json = (cdir / "change.json").read_bytes()
        except FileNotFoundError:
            continue
        try:
            meta = json.loads(change_json)
        except json.JSONDecodeError:
            continue
        if meta.get("materialized", True) is False and meta.get("origin", "draft") != "draft":
            continue  # un-materialized ideas/follow-ups belong to the Backlog zone
        last = _last_event_iso(cdir / "events.jsonl") or ""
        row = _status_row(change_id, state_json, change_json, last)
        if row is None:
            continue
        status = _draft_label(_effective_status(row[1], cdir), change_json)
        row = (row[0], status, *row[2:])
        if row[1] not in _INFLIGHT:
            continue
        rows.append(row)
    return rows


def _completed_rows(control: Path) -> list[_Row]:
    """Completed/abandoned Changes NOT currently surfaced in-flight, from a
    SINGLE indexed archive query (spec AC 7)."""
    live = {r[0] for r in _status_rows(control)}
    rows: list[_Row] = []
    for row in archive_read.completed_rows(control):
        if row[0] in live or row[1] in _INFLIGHT:
            continue
        rows.append(row)
    return rows


def _sort_rows_newest_first(rows: list[_Row]) -> list[_Row]:
    return sorted(rows, key=lambda r: (r[3], r[5]), reverse=True)


def _resumable_rows(control: Path) -> list[_Row]:
    rows = [r for r in _status_rows(control) if r[1] in RESUMABLE]
    rows += _backlog_rows(control)
    return _sort_rows_newest_first(rows)


def _past_change_count(control: Path) -> int:
    return len(_completed_rows(control))


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _resume_picker_empty(control: Path) -> int:
    print("Nothing to resume.")
    past = _past_change_count(control)
    if past:
        plural = "s" if past != 1 else ""
        print(pal.ansi(
            f"({past} past change{plural} — see `imp ls --all`)",
            "dim", enabled=should_use_color(sys.stdout)))
    return 0


def _resume_picker_non_tty(rows: list[_Row]) -> str | int:
    if len(rows) == 1:
        return rows[0][0]
    print("Multiple resumable Changes — pass a token:", file=sys.stderr)
    color = should_use_color(sys.stderr)
    now = datetime.now(UTC)
    print(change_row.header_line(color=color), file=sys.stderr)
    name_w = change_row.name_budget()
    for r in rows:
        print(change_row.format_change_line(
            change_id=r[0], title=r[4], status=r[1], step=r[2], step_label=r[6],
            last_iso=r[3], now=now, color=color, name_width=name_w), file=sys.stderr)
    return 2


async def _pick_change_target(
    *, rows: list[_Row], header: str,
    on_empty: Callable[[], int],
    on_non_tty: Callable[[list[_Row]], str | int] | None = None,
    selected_index: int = 0,
    non_interactive: bool = False,
) -> str | int:
    if not rows:
        return on_empty()
    if non_interactive or not _interactive():
        assert on_non_tty is not None
        return on_non_tty(rows)
    if len(rows) == 1:
        return rows[0][0]
    candidates = [resume_select.Candidate(
        change_id=r[0], title=r[4], status=r[1], step=r[2], last_iso=r[3],
        step_label=r[6])
        for r in rows]
    chosen = await resume_select.run_picker(
        candidates, now=datetime.now(UTC),
        color=should_use_color(sys.stdout),
        header=header, selected_index=selected_index)
    return chosen if chosen is not None else 2


async def _pick_resume_target(control: Path, *, non_interactive: bool) -> str | int:
    return await _pick_change_target(
        rows=_resumable_rows(control),
        header="Resume which change?  (↑/↓ to move · enter to resume · q to cancel)",
        on_empty=lambda: _resume_picker_empty(control),
        on_non_tty=_resume_picker_non_tty,
        non_interactive=non_interactive,
    )


async def _pick_playbook(control: Path, *, non_interactive: bool) -> str | int:
    try:
        pbs = playbooks.list_playbooks(control)
    except ValueError as e:
        print(f"imp: {e}", file=sys.stderr)
        return 2
    if not pbs:
        print("No playbooks found.", file=sys.stderr)
        return 2
    if len(pbs) == 1:
        return pbs[0].name
    if non_interactive or not _interactive():
        print("Multiple playbooks — pass a name:", file=sys.stderr)
        for pb in pbs:
            print(f"  {pb.name} — {pb.description}", file=sys.stderr)
        return 2
    chosen = await resume_select.run_item_picker(
        pbs,
        render_item=lambda pb: f"{pb.name} — {pb.description}",
        header="Run which playbook?  (↑/↓ to move · enter to run · q to cancel)",
        color=should_use_color(sys.stdout))
    return chosen.name if chosen is not None else 2


def _deliver_abandon_resident(control: Path, change_id: str) -> None:
    from imp.remote.session.client import SessionClient
    from imp.remote.session.discover import session_socket_path
    from imp.remote.session.protocol import CONTROL_ABANDON

    async def _run() -> None:
        path = str(session_socket_path(control, change_id))
        client = SessionClient(path, change_id, "", on_event=lambda _: None)
        await client.connect()
        try:
            await client.send_control(CONTROL_ABANDON)
            with anyio.fail_after(10):
                await client.receive_events()
        finally:
            await client.aclose()

    anyio.run(_run)


def _abandon(control: Path, change_id: str, *, assume_yes: bool = False,
             interactive: bool | None = None,
             input_fn: Callable[[str], str] = input) -> int:
    """Tear down a Change (worktree, branch, state) as one logged operation.

    Order is pinned by git (spec §2): archive the runtime snapshot BEFORE
    worktree removal (records live inside the worktree); `git branch -D` AFTER
    (git refuses to delete a branch checked out in a worktree). Idempotent /
    crash-tolerant: re-running after any mid-sequence crash converges."""
    try:
        change = Change.load(control, change_id)
    except (LoadError, LoadErrors):
        # No live or un-materialized change-home: Change.load failed because the
        # home directory is absent (already fully torn down, or never existed).
        # Idempotent teardown — reap a possibly-orphaned branch and exit 0.
        worktree.delete_branch(control, change_id)
        print(f"Change {change_id} already gone; ensured branch imp/{change_id} is removed.")
        return 0

    token = change.id
    do_prompt = not assume_yes and (
        _interactive() if interactive is None else interactive)
    if do_prompt:
        st = State.load(change.state_dir)
        effective = _effective_status(st.status, change.change_dir)
        n = worktree.commit_count(control, change_id, change.base_branch)
        print(f"Abandon {token}: {change.title!r} "
              f"(status {effective}, {n} commit{'s' if n != 1 else ''})?")
        try:
            ans = input_fn(
                "Type 'y' to confirm, anything else to cancel: ").strip().lower()
        except EOFError:
            ans = ""
        if ans not in ("y", "yes"):
            print(f"Abandon cancelled; {token} left intact.")
            return 1

    if session_is_resident(control, change_id):
        try:
            _deliver_abandon_resident(control, change_id)
        except OSError:
            pass  # daemon gone, wedged, or startup-window hang (TimeoutError ⊂ OSError)
        else:
            print(f"Abandoned {token}.")
            return 0
    try:
        tear_down_change(change)
    except LockHeld as e:
        print(f"imp: Change {change_id} is running (pid {e.held_by_pid or '?'}); "
              f"stop it before abandoning", file=sys.stderr)
        return 2
    print(f"Abandoned {token}.")
    return 0


def _cmd_remote_control_start(args: argparse.Namespace) -> int:
    from imp.remote.daemon.runner import start_daemon
    return start_daemon(_control_root())


def _cmd_remote_control_stop(args: argparse.Namespace) -> int:
    from imp.remote.daemon.runner import stop_daemon
    return stop_daemon(_control_root())


def _abandon_picker_empty() -> int:
    print("Nothing to abandon.")
    return 2


def _abandon_picker_non_tty(rows: list[_Row]) -> int:
    print("imp: abandon needs a change token on a non-interactive terminal",
          file=sys.stderr)
    return 2


async def _pick_abandon_target(control: Path) -> str | int:
    return await _pick_change_target(
        rows=_sort_rows_newest_first(_status_rows(control)),
        header="Abandon which change?  (↑/↓ to move · enter to select · q to cancel)",
        on_empty=_abandon_picker_empty,
        on_non_tty=_abandon_picker_non_tty,
    )


def _cmd_abandon(args: argparse.Namespace) -> int:
    control = _control_root()
    if args.token is None:
        target = anyio.run(_pick_abandon_target, control)
        if isinstance(target, int):
            return target
        return _abandon(control, target, assume_yes=args.yes)
    try:
        change_id = _resolve_id(control, args.token)
    except SystemExit:
        # _resolve_id scans live worktrees only. A Change whose worktree was
        # already removed (crash between abandon's remove and branch-delete)
        # is unresolvable there — but abandon archived the id before removal,
        # so recover it from the archive to still reap an orphaned branch
        # (AC #12). Exact id, else unique suffix match.
        ids = archive_read.archived_ids(control)
        token = args.token
        hits = [cid for cid in ids if cid == token] or \
               [cid for cid in ids if cid.endswith(token)]
        if len(hits) == 1:
            return _abandon(control, hits[0], assume_yes=args.yes)
        # Truly unknown (never existed, or ambiguous) — nothing to reap.
        print(f"imp: no Change matches {token!r}", file=sys.stderr)
        return 2
    return _abandon(control, change_id, assume_yes=args.yes)


def _cmd_artifact_commit(args: argparse.Namespace) -> int:
    control = _control_root()
    change_id = os.environ.get("IMP_CHANGE_ID")
    if not change_id:
        print("imp: IMP_CHANGE_ID not set", file=sys.stderr)
        return 1
    change = Change.load(control, change_id)
    art_root = change.change_dir / "artifacts"
    files = {
        str(p.relative_to(art_root)): p
        for p in sorted(art_root.rglob("*")) if p.is_file()
    }
    if not files:
        print(f"imp: no artifacts under {art_root}", file=sys.stderr)
        return 1
    worktree.artifact_commit(control, change_id, files,
                             message=f"artifact: {args.kind}")
    return 0


def _backlog_rows(control: Path) -> list[_Row]:
    rows: list[_Row] = []
    for change_id in _list_change_homes(control):
        cdir = _change_home(control, change_id)
        try:
            change_json = (cdir / "change.json").read_bytes()
            meta = json.loads(change_json)
        except (FileNotFoundError, json.JSONDecodeError):
            continue
        if meta.get("materialized", True):
            continue  # materialized homes are Active, not Backlog
        origin = meta.get("origin", "draft")
        if origin == "draft":
            continue  # un-materialized drafts are Active, not Backlog
        ready, _unmerged = backlog.readiness(control, meta.get("depends_on", []))
        last = _last_event_iso(cdir / "events.jsonl") or ""
        created = meta.get("created_at", "")
        blocked = "" if ready else "blocked"
        rows.append((change_id, origin, blocked, last,
                     meta.get("title") or "", created, blocked or None))
    return rows


def _cmd_status(args: argparse.Namespace, _control: Path | None = None) -> int:
    try:
        control = _control if _control is not None else _control_root()
    except worktree.PreconditionError:
        print(_NO_CHANGES_MESSAGE)
        return 0
    now = datetime.now(UTC)
    inflight = _sort_rows_newest_first(_status_rows(control))
    backlog_rows = _sort_rows_newest_first(_backlog_rows(control))
    past = _sort_rows_newest_first(_completed_rows(control)) if args.all else []
    if not inflight and not backlog_rows and not past:
        print(_NO_CHANGES_MESSAGE)
        return 0
    color = should_use_color(sys.stdout)
    name_w = change_row.name_budget()
    print(change_row.header_line(color=color))
    for r in inflight:
        intent = None
        if not r[4].strip():
            intent = _resolve_intent(_load_events_for_id(control, r[0]) or [])
        print(change_row.format_change_line(
            change_id=r[0], title=r[4], status=r[1], step=r[2], step_label=r[6],
            last_iso=r[3], now=now, color=color, name_width=name_w, intent=intent))
    if backlog_rows:
        print()
        print(pal.ansi("Backlog", "dim", enabled=color))
        for r in backlog_rows:
            print(change_row.format_change_line(
                change_id=r[0], title=r[4], status=r[1], step=r[2], step_label=r[6],
                last_iso=r[3], now=now, color=color, name_width=name_w))
    if past:
        if inflight or backlog_rows:
            print()
        for r in past:
            print(change_row.format_change_line(
                change_id=r[0], title=r[4], status=r[1], step=r[2], step_label=r[6],
                last_iso=r[3], now=now, color=color, name_width=name_w, dim=True))
    return 0


def _cmd_search(args: argparse.Namespace, _control: Path | None = None) -> int:
    control = _control if _control is not None else _control_root()
    hits = archive_read.search_archive(control, args.query)
    if not hits:
        print(f"No archived changes match {args.query!r}.")
        return 0
    for change_id, title, status, _created in hits:
        print(f"{change_id}  {title or '(untitled)'}  [{status or '-'}]")
    return 0


def _cmd_config(args: argparse.Namespace) -> int:
    from imp.cli.config_cmd import render_config, render_explain
    from imp.providers.effective import load_effective_config
    # _project_root() never raises (returns the resolved start dir when no marker
    # is found); only _control_root()'s git resolution can fail (not in a work
    # tree). Resolve project first, then guard only the control read.
    root = _project_root()
    try:
        control = _control_root()
    except Exception:
        control = root  # no git work tree → local `.imp/config.yaml` layer is skipped
    eff = load_effective_config(project_root=root, control_root=control)
    explain = getattr(args, "explain", None)
    if explain:
        render_explain(eff, explain, sys.stdout, color=should_use_color(sys.stdout))
    else:
        render_config(eff, sys.stdout, color=should_use_color(sys.stdout),
                      show_all=args.all)
    return 0


_OVERVIEW_COMMANDS: tuple[tuple[str, str], ...] = (
    ("chat", "Open a conversation with imp — ask questions, scout work, or scope a change."),
    ("new", "Scope a new change in conversation (optional opening message)."),
    ("resume", "Continue an in-flight Change (the sole resumable one, else a picker)."),
    ("ls", "List your changes (-a also shows completed and abandoned)."),
    ("log", "Show a Change's narrative as plain text."),
    ("abandon", "Tear down a Change's worktree, branch, and state."),
    ("config", "Show the effective configuration."),
)


def render_overview(color: bool) -> str:
    head = ansi("imp", "bold", enabled=color)
    width = max(len(name) for name, _ in _OVERVIEW_COMMANDS)
    lines = [
        f"{head} — multi-agent software development workflow",
        "",
        "Common commands:",
    ]
    for name, desc in _OVERVIEW_COMMANDS:
        lines.append(f"  {name.ljust(width)}  {desc}")
    lines += [
        "",
        "New to a project? Run 'imp init' to get started.",
        "Run 'imp <command> -h' for details.",
    ]
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="imp")
    parser.add_argument("--version", action="version", version=f"imp {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=False)

    p_init = sub.add_parser(
        "init",
        help="Set up imp here (initializes git if needed).",
        description=(
            "Set up imp in this directory: optionally initialize a git\n"
            "repository, write imp/project.yaml (the version-controlled\n"
            "workflow + project settings) with the default workflow, detect\n"
            "your terminal theme, and bootstrap your machine-local config\n"
            "(providers and theme). Makes an initial commit of\n"
            "imp/project.yaml and .gitignore."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_init.set_defaults(func=_cmd_init)

    p_new = sub.add_parser(
        "new", help="Start a new Change: run the workflow from an opening intent.")
    p_new.add_argument(
        "opening", nargs="?", metavar="MESSAGE",
        help="Opening intent handed verbatim to the spec-writer as its first "
             "message; omit to start with an interactive prompt. Distinct from "
             "--message, which is a headless reply to the first solicit.")
    p_new.add_argument("--title", metavar="TEXT", default=None,
                       help="Set the Change title explicitly; the spec-writer "
                            "names the Change if omitted.")
    p_new.add_argument("--non-interactive", action="store_true",
                       help="Headless: no live prompt; supply the reply via --message.")
    p_new.add_argument("--message", metavar="TEXT", default=None,
                       help="Headless reply consumed by the first wait_for_user; "
                            "pause+resume with another --message. "
                            "Requires --non-interactive.")
    p_new.add_argument("--spec-file", metavar="PATH", default=None,
                       help="Seed the change from a draft spec file: its content "
                            "becomes the spec-writer's opening intent. A missing or "
                            "unreadable PATH is a hard error (rc 2). Mutually "
                            "exclusive with a positional MESSAGE and with --message.")
    p_new.add_argument("--playbook", metavar="NAME", default=None,
                       help="Seed the change from imp/playbooks/<NAME>.md, "
                            "rendering {{ args }} from the positional MESSAGE.")
    p_new.add_argument("--findings-only", action="store_true",
                       help="With --playbook, stop at a committed findings doc "
                            "instead of implementing.")
    p_new.add_argument("--tui", action="store_true",
                       help="Render the run in a full-screen, scrollable viewer "
                            "(expanded steps); falls back to streaming on a non-tty.")
    p_new.add_argument("--idea", metavar="TITLE", default=None,
                       help="Jot a backlog Change with this TITLE WITHOUT opening a "
                            "conversation; prints its id. Add --description for a "
                            "longer brief. Start it later with `imp resume <id>`.")
    p_new.add_argument("--description", metavar="TEXT", default=None,
                       help="Optional longer description for an --idea Change "
                            "(may be empty).")
    p_new.add_argument("--depends-on", metavar="ID", action="append",
                       default=None, dest="depends_on",
                       help="A change id this backlog Change depends on "
                            "(repeatable). Gates ready/blocked; never changes the "
                            "base branch.")
    p_new.add_argument("--workflow", metavar="NAME", default=None,
                       help="Run this Change on workflow NAME instead of the "
                            "project default. A bundled name needs no commit; a "
                            "committed imp/workflows/NAME fork overrides it.")
    p_new.set_defaults(func=_cmd_new)

    p_chat = sub.add_parser(
        "chat",
        help="Open a conversation with imp (ask questions, scout work, or "
             "scope a change).")
    p_chat.add_argument(
        "opening", nargs="?", metavar="MESSAGE",
        help="Optional opening message; omit to start with an interactive prompt.")
    p_chat.add_argument("--non-interactive", action="store_true",
                        help="Headless: no live prompt; supply the reply via --message.")
    p_chat.add_argument("--message", metavar="TEXT", default=None,
                        help="Headless reply consumed by the first wait_for_user; "
                             "requires --non-interactive.")
    p_chat.add_argument("--tui", action="store_true",
                        help="Render the conversation in a full-screen, scrollable "
                             "viewer; falls back to streaming on a non-tty.")
    p_chat.set_defaults(func=_cmd_chat)

    p_run = sub.add_parser(
        "run", help="Run a playbook by name (or pick one interactively).")
    p_run.add_argument(
        "name", nargs="?",
        help="Playbook name (resolves imp/playbooks/<name>.md); omit to pick "
             "from a list.")
    p_run.add_argument(
        "playbook_args", nargs="?", metavar="ARGS",
        help="Trailing argument exposed to the playbook body as {{ args }}.")
    p_run.add_argument("--findings-only", action="store_true",
                       help="Stop at a committed findings doc instead of "
                            "implementing.")
    p_run.add_argument("--title", metavar="TEXT", default=None,
                       help="Set the Change title explicitly.")
    p_run.add_argument("--non-interactive", action="store_true",
                       help="Headless: no live prompt; supply the reply via --message.")
    p_run.add_argument("--message", metavar="TEXT", default=None,
                       help="Headless reply consumed by the first wait_for_user; "
                            "requires --non-interactive.")
    p_run.add_argument("--tui", action="store_true",
                       help="Render the run in a full-screen, scrollable viewer; "
                            "falls back to streaming on a non-tty.")
    p_run.set_defaults(func=_cmd_run)

    p_resume = sub.add_parser(
        "resume", help="Resume a paused or interrupted Change by id or token.")
    p_resume.add_argument(
        "token", nargs="?",
        help="Change id or resume token identifying which Change to resume.")
    p_resume.add_argument("--non-interactive", action="store_true",
                          help="Headless: no live prompt; supply the reply via --message.")
    p_resume.add_argument("--message", metavar="TEXT", default=None,
                          help="Headless reply consumed by the first wait_for_user; "
                               "pause+resume with another --message. "
                               "Requires --non-interactive.")
    p_resume.add_argument("--tui", action="store_true",
                          help="Render the run in a full-screen, scrollable viewer "
                               "(expanded steps); falls back to streaming on a non-tty.")

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
    p_log.add_argument(
        "token", nargs="?",
        help="Change id or its short token (omit for the most recent Change).")
    p_log_mode = p_log.add_mutually_exclusive_group()
    p_log_mode.add_argument(
        "--full", action="store_true",
        help="Render every event (no tool-burst collapse).",
    )
    p_log_mode.add_argument(
        "-s", "--summary", action="store_true",
        help="Compact run-overview timeline: one line per step occurrence, "
             "grouped by phase, with wall-clock durations.",
    )
    p_log.add_argument(
        "--no-pager", action="store_true",
        help="Disable the pager even when stdout is a TTY.",
    )
    p_log.set_defaults(func=_cmd_log)

    p_show = sub.add_parser(
        "show",
        help="Show a Change's spec, plan, review, events, or status.",
        description=(
            "Print one artifact of a Change. Reads the live worktree files while "
            "the Change is in flight, and falls back to the archive once it is "
            "merged and the worktree is gone."),
    )
    p_show.add_argument("token", help="Change id or its short token.")
    p_show.add_argument(
        "resource", nargs="?", default=None,
        choices=["spec", "plan", "review", "events", "status"],
        help="Which artifact to print (default: a detail card).")
    p_show.set_defaults(func=_cmd_show)

    p_ls = sub.add_parser(
        "ls", aliases=["list"],
        help="List your changes (use -a for completed and abandoned).",
        description=(
            "List your in-progress changes and the Backlog (saved-for-later "
            "changes). Use -a/--all to also show completed and abandoned changes."
        ),
    )
    p_ls.add_argument(
        "-a", "--all", action="store_true",
        help="Also list completed and abandoned changes "
             "(default: in-progress + Backlog).",
    )
    p_ls.set_defaults(func=_cmd_status)

    p_search = sub.add_parser(
        "search",
        help="Search archived changes by title or id.",
        description=(
            "Search the archive (completed, abandoned, and left conversations) "
            "for changes whose title or id contains the query substring."),
    )
    p_search.add_argument(
        "query", metavar="QUERY",
        help="Substring matched case-insensitively against a change's title or id.")
    p_search.set_defaults(func=_cmd_search)

    p_config = sub.add_parser(
        "config",
        help="Show the effective config and per-setting provenance.",
        description=(
            "Show the effective configuration and where each setting came "
            "from (its provenance layer). --explain TIER traces one tier's "
            "model bindings through the layers; --all shows every "
            "provider x tier row."
        ),
    )
    p_config.add_argument("--explain", metavar="TIER", default=None,
                          help="Trace how one tier's model bindings resolve "
                               "through the configuration layers.")
    p_config.add_argument("--all", action="store_true",
                          help="Show every provider x tier row "
                               "(default: only configured providers).")
    p_config.set_defaults(func=_cmd_config)

    p_abandon = sub.add_parser(
        "abandon",
        help="Tear down a Change's worktree, branch, and state (logged).",
    )
    p_abandon.add_argument(
        "token", nargs="?",
        help="Change id or short token identifying which Change to abandon "
             "(omit on a tty to pick from a list).")
    p_abandon.add_argument(
        "-y", "--yes", action="store_true",
        help="Skip the confirmation prompt (also skipped on a non-interactive stdin).")
    p_abandon.set_defaults(func=_cmd_abandon)

    p_artifact = sub.add_parser(
        "artifact-commit",
        help="Commit the change's live artifacts to its journal-wip ref "
             "(invoked by artifact-commit.sh; not for direct use).")
    p_artifact.add_argument("kind", choices=["spec", "plan"],
                            help="Artifact kind being committed (labels the commit).")
    p_artifact.set_defaults(func=_cmd_artifact_commit)

    p_rc = sub.add_parser(
        "remote-control",
        help="Run the optional per-repo daemon that relays all Changes to one transport.",
    )
    rc_sub = p_rc.add_subparsers(dest="rc_action", required=True)
    p_rc_start = rc_sub.add_parser(
        "start", help="Start the remote-control daemon for this repo (foreground).")
    p_rc_start.set_defaults(func=_cmd_remote_control_start)
    p_rc_stop = rc_sub.add_parser(
        "stop", help="Stop the running remote-control daemon (SIGTERM).")
    p_rc_stop.set_defaults(func=_cmd_remote_control_stop)

    return parser


def _command_names(parser: argparse.ArgumentParser) -> set[str]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return set(action.choices)
    return set()


def _help_command(rest: list[str], parser: argparse.ArgumentParser) -> int:
    if not rest:
        _apply_theme()
        print(render_overview(should_use_color(sys.stdout)))
        return 0
    cmd = rest[0]
    if cmd not in _command_names(parser):
        print(f"imp: unknown command {cmd!r}. Run 'imp help'.", file=sys.stderr)
        return 2
    # `imp help <cmd>` == `imp <cmd> -h`: argparse prints the subparser help and
    # exits 0 (SystemExit), so control never returns past this line.
    parser.parse_args([cmd, "-h"])
    return 0


def _dispatch_meta(args: list[str], parser: argparse.ArgumentParser) -> int | None:
    if not args:
        _apply_theme()
        print(render_overview(should_use_color(sys.stdout)))
        return 0
    if args[0] == "help":
        return _help_command(args[1:], parser)
    first = args[0]
    if not first.startswith("-") and first not in _command_names(parser):
        print(f"imp: unknown command {first!r}. Run 'imp help'.", file=sys.stderr)
        return 2
    return None


def _init_logging() -> None:
    """Claim the root logger before any FastMCP instantiation can hijack it.

    `FastMCP.__init__` calls `configure_logging()`, which runs
    `logging.basicConfig(level=INFO, handlers=[RichHandler(stderr)])` — but only
    when the root logger has no handlers yet. imp instantiates `FastMCP(...)` for
    its in-process tool servers (impty at `impty/mcp.py`, codex at
    `providers/codex_mcp.py`), so the first such instantiation would otherwise
    install a stderr RichHandler at INFO and paint propagated third-party INFO
    logs — notably the Claude Agent SDK's "Using bundled Claude Code CLI" — over
    the live TUI. We pre-empt that by owning root logging here, before the
    command path imports any of those modules.

    `IMP_DEBUG=1` opts into stderr logging for development; otherwise records are
    dropped via a NullHandler so nothing corrupts the terminal UI. Named-logger
    handlers added elsewhere (e.g. the daemon's `imp.daemon`/`imp.remote` file
    handlers) are untouched — they attach to their own loggers, not root.
    """
    root = logging.getLogger()
    if root.handlers:
        return  # already configured (e.g. by an embedding host) — don't clobber
    if os.environ.get("IMP_DEBUG") == "1":
        handler: logging.Handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
    else:
        root.addHandler(logging.NullHandler())
        root.setLevel(logging.WARNING)
        # Belt-and-suspenders: keep noisy third-party INFO chatter off any
        # handler a dependency might still attach to its own logger.
        for name in ("claude_agent_sdk", "mcp", "FastMCP"):
            logging.getLogger(name).setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    _init_logging()
    args = sys.argv[1:] if argv is None else argv
    parser = _build_parser()
    try:
        result = _dispatch_meta(args, parser)
        if result is not None:
            # _dispatch_meta may have printed the overview/unknown-command
            # error above; that write belongs inside the guard too. Flush here
            # for the same buffered-pipe reason as the command path below.
            sys.stdout.flush()
            return result
        ns = parser.parse_args(args)
        _apply_theme()   # resolve dark/light/ansi once, before any command renders
        rc: int = ns.func(ns)
        # Flush here, inside the guard: output small enough to fit Python's
        # stdout buffer is otherwise flushed at interpreter shutdown, where a
        # broken pipe surfaces as an uncatchable "Exception ignored" traceback.
        # Forcing the flush now moves that write where the handler can catch it.
        sys.stdout.flush()
        return rc
    except worktree.PreconditionError as e:
        print(f"imp: {e}", file=sys.stderr)
        return 2
    except BrokenPipeError:
        # A reader (e.g. `imp log | less` quit before draining) closed the pipe.
        # Redirect stdout to devnull so the interpreter's final flush can't
        # re-raise during shutdown, then exit with the conventional SIGPIPE code.
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        return 128 + signal.SIGPIPE
