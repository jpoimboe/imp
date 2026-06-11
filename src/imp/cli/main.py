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

from imp.agents.tools.wait_for_user import AwaitingUserInput
from imp.cli import palette as pal
from imp.cli.init import run_init
from imp.cli.live_driver import LiveDriver
from imp.cli.live_status import LiveStatus
from imp.cli.palette import ansi, should_use_color
from imp.cli.prompt import Prompt
from imp.cli.remote_channel import (
    RemoteChannel,
    compose_on_event,
    driver_config_from,
    effective_reply_fn,
)
from imp.cli.signals import EscKeyReader, install_process_group, pause_and_exit
from imp.cli.slash import ask_line_with_slash
from imp.engine import journal, recovery, worktree
from imp.engine.change import Change
from imp.engine.errors import LoadError, LoadErrors
from imp.engine.executor import RunResult, run_change
from imp.engine.project_root import find_project_root
from imp.engine.slug import dated_slug, new_change_id
from imp.engine.state import LockHeld, ProjectLock, State


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
    (so `reply_sink` is rarely exercised: while a solicit is active the ESC
    reader is quiesced and never funnels a line in). The flag still guards the
    race where a sink call lands during a prompt — that line is then a reply, not
    a stray interject.

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


def _maybe_build_channel(change: Change) -> RemoteChannel | None:
    """Build a RemoteChannel iff a `remote.driver` is configured, else None (imp
    behaves exactly as today). Title is captured at run start. A bad config is
    surfaced elsewhere (run_change reloads it) — never fail channel construction
    over config here; return None on any config error."""
    from imp.providers.effective import load_effective_config
    try:
        config = load_effective_config(
            project_root=change.project_root,
            control_root=change.control_root).config
    except (LoadError, LoadErrors):
        return None
    dc = driver_config_from(config)
    if dc is None:
        return None
    return RemoteChannel.from_driver_config(dc, id=change.id, title=change.title)


def _build_interactive_input(
    change: Change, status: LiveStatus, coord: Any, esc_reader: Any, driver: Any,
) -> tuple[ReplyInterjectRouter, Callable[[], Awaitable[str]]]:
    """Build the interactive input plumbing shared by `new` and `resume` (Task 11).

    Returns `(router, solicit_fn)`:
      - `router` is the testable routing core (reply-vs-interject by submit-time
        state, see ReplyInterjectRouter);
      - `solicit_fn` is what the engine calls to SOLICIT a reply (wait_for_user):
        it marks the router's solicit flag active for the duration
        of the prompt_toolkit read and returns the typed line. Same behavior as
        the legacy `_ask_line` — the prompt owns the tty (ESC quiesced) and the
        line is the REPLY.

    Where do interject lines come from? NOT a competing prompt — that would steal
    /dev/tty from the ESC reader during agent-thinking and break ESC-to-pause (the
    landed invariant). Instead the ESC reader — the sole /dev/tty owner during
    thinking — gets an `on_interject` sink: a line typed while the agent runs is
    captured raw and handed to `router.route`, which (solicit inactive) enqueues
    it as an interject. During a solicit the ESC reader is quiesced and silent, so
    its sink never fires then; the prompt owns input and the line is a reply.

    The solicit-active flag is thus realized two ways that agree: the router's
    boolean (set around the prompt read) and, physically, the ESC reader's
    quiesce state. `router.route` is the pure, unit-tested decision; this wiring
    is the thin, TTY-fragile shell around it (the interject read path is the one
    documented to CPR-degrade under impty — L4).
    """
    prompt = Prompt(skills_dir=change.workflow_dir / "skills",
                    editing_mode=_editing_mode(change.control_root),
                    color=should_use_color(sys.stdout))
    router = ReplyInterjectRouter(coord)

    # The ESC reader funnels a mid-run typed line here; route() enqueues it as an
    # interject (no solicit active) — delivered at the next agent turn boundary.
    esc_reader.set_interject_sink(router.route)

    async def _solicit() -> str:
        # Reply path: identical to the legacy _ask_line, but flag the router
        # active for the prompt's duration so the reply-vs-interject model is
        # explicit (and a stray sink call during a prompt would route as reply).
        router.begin_solicit()
        try:
            return await ask_line_with_slash(
                prompt, status, change,
                color=should_use_color(sys.stdout), width=_term_width(),
                coord=coord, driver=driver)
        finally:
            router.end_solicit()

    return router, _solicit


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
    """A script step paused (e.g. integrate-merge exit 3/4/5, base-fresh-check exit 1).
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


def _should_respawn_reader(*, reader_exited: bool) -> bool:
    """The ESC reader exits ONLY when ESC fired (run() returns); after a Ctrl-C
    interrupt it is still alive (parked) and reset() un-parks it. So respawn iff
    the previous round's reader exited. This reader-exit signal — NOT the
    coordinator's is_running() — is the correct ESC-exit-vs-Ctrl-C-park
    discriminator (review M1)."""
    return reader_exited


def _interactive_loop_predicate(
    *, non_interactive: bool, reader_armed: bool,
    stdin_tty: bool, stdout_tty: bool,
) -> bool:
    """The §2 loop runs only for an attended session: not --non-interactive, the
    ESC reader armed at startup (LATCHED — not a live is_running(), which reads
    false right after an ESC interrupt), and both stdin/stdout are ttys (the
    reader opens /dev/tty, so a piped stdin must not park a run at a prompt
    consuming pipe content as replies)."""
    return (not non_interactive) and reader_armed and stdin_tty and stdout_tty


def _handle_terminal_outcome(
    change: Change, result: RunResult, coord: Any,
) -> int:
    """Render the terminal outcome of a settled run and return its rc.

    COMPLETED → green completion line (rc 0). PAUSED → merge_blocked /
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
    elif final_state.step is not None:
        # Surface the LAST script_blocked for the paused step (keyed on step name
        # to avoid stale events from earlier pauses that were subsequently
        # resumed; emitted once).
        matched = _last_script_blocked(
            list(change.event_log().read()), final_state.step)
        if matched is not None:
            _emit_script_blocked(
                final_state.step,
                (matched.get("payload") or {}).get("stderr", ""),
            )
    _emit_paused_summary(change, result.value)
    return 1


async def _run_actuator(
    coord: Any, exit_event: Any, run_scope_holder: dict[str, Any],
) -> None:
    """Watch the escalation + exit events; on either, cancel ONLY the current
    round's run scope (held in run_scope_holder) — never the task group, which
    owns the signal receiver + status ticker the exit path still needs. On
    escalation, also set coord.escalated so the loop routes to force-quit."""

    async def _on_esc() -> None:
        await coord.escalation_event.wait()
        coord.escalated = True
        sc = run_scope_holder["scope"]
        if sc is not None:
            sc.cancel()

    async def _on_exit() -> None:
        await exit_event.wait()
        sc = run_scope_holder["scope"]
        if sc is not None:
            sc.cancel()

    async with anyio.create_task_group() as wtg:
        wtg.start_soon(_on_esc)
        wtg.start_soon(_on_exit)


async def _run_change_session(
    change: Change,
    *,
    coord: Any,
    driver: Any,
    status: Any,
    esc_reader: EscKeyReader | None,
    reply_input_fn: Callable[[], Awaitable[str]],
    non_interactive: bool,
    tg: Any,
    channel: Any = None,
) -> int:
    """The shared run+handle block for `new`/`resume`, wrapped in the §2 session
    loop: a user-interrupt pause (ESC / Ctrl-C) re-prompts in place instead of
    exiting. Runs INSIDE the caller's task group (`tg`).

    Each round runs `run_change` under a FRESH single-use CancelScope (published
    to `run_scope_holder` so the C4 actuator can cancel the live run on escalation
    / exit-event). On a loopable interrupt pause the coordinator is reset, the ESC
    reader respawned if it exited, and a prompt collects the next message before
    the next round; a bare Enter with an empty queue re-prompts. Out-of-band stops
    (Ctrl-D, force-quit, SIGTERM/exit-event) finalize a paused exit (rc 130). On
    every exit path the live status is reset and the task group cancelled."""
    # C4 actuator scaffolding + ack hook (in scope for the whole session).
    coord.escalation_event = anyio.Event()
    exit_event = anyio.Event()
    coord._exit_event = exit_event   # the D3 prompt-teardown race reads this
    coord.set_escalation_hook(coord.escalation_event.set)
    coord.set_exit_hook(lambda _r: exit_event.set())  # only signals; pause
    # state is set on the signal paths themselves (review B2)
    if not non_interactive:
        coord.set_ack_hook(status.show_stop_ack)
    run_scope_holder: dict[str, Any] = {"scope": None}

    tg.start_soon(_run_actuator, coord, exit_event, run_scope_holder)

    try:
        if channel is not None and not await channel.start(tg):
            channel = None
        run_on_event = (
            compose_on_event(driver.on_event, channel.on_event)
            if channel is not None else driver.on_event)
        reader_armed = (
            bool(esc_reader and esc_reader._armed) if not non_interactive else False)
        result: RunResult | None = None
        while True:
            run_scope_holder["scope"] = anyio.CancelScope()
            try:
                with run_scope_holder["scope"]:
                    # Bind the CURRENT reply_input_fn (it is rebound on reader
                    # respawn each round) so the wait_for_user channel always
                    # calls the live one.
                    _reply: Callable[[], Awaitable[str]] = effective_reply_fn(
                        reply_input_fn, channel, non_interactive=non_interactive)

                    def _chat(
                        _q: object,
                        _fn: Callable[[], Awaitable[str]] = _reply,
                    ) -> Awaitable[str]:
                        return _fn()

                    result = await run_change(
                        change, on_event=run_on_event,
                        user_chat_input_fn=_chat,
                        coordinator=coord, non_interactive=non_interactive)
            except EOFError:
                return _finalize_paused_exit(change, driver, coord, "ctrl-d")
            except KeyboardInterrupt:
                return _finalize_paused_exit(change, driver, coord, "ctrl-c")

            if coord.escalated:
                return _finalize_paused_exit(change, driver, coord, "force-quit")
            if exit_event.is_set():
                return _finalize_paused_exit(
                    change, driver, coord, coord.pause_reason or "sigterm")
            assert result is not None

            interrupt_paused = (
                result is RunResult.PAUSED
                and coord.should_pause
                and not coord.escalated)
            if not interrupt_paused:
                # Terminal outcome (COMPLETED, or a non-interrupt PAUSE such as
                # merge_blocked). Do NOT reload State here: a merge-completing run
                # has already finalized in _complete by removing the worktree —
                # the change state dir, and its state.json, went with it — so
                # State.load(change.state_dir) would crash with FileNotFoundError.
                # final_state is only ever consulted on the interrupt-paused
                # loopable path below, so it is never needed here. The terminal
                # handler reads the outcome from `result`, not from disk.
                break
            final_state = State.load(change.state_dir)
            interactive = _interactive_loop_predicate(
                non_interactive=non_interactive, reader_armed=reader_armed,
                stdin_tty=sys.stdin.isatty(), stdout_tty=sys.stdout.isatty())
            loopable = (
                coord.pause_reason in ("esc", "ctrl-c")
                and final_state.status == "paused"
                and interactive)
            if not loopable:
                # An interrupt paused the run but the session is not loopable
                # (--non-interactive, or no controlling tty): exit paused,
                # mirroring the legacy `coord.should_pause` arm (rc 130).
                return _finalize_paused_exit(
                    change, driver, coord, coord.pause_reason or "ctrl-c")

            coord.reset()
            if not non_interactive and _should_respawn_reader(
                    reader_exited=(coord._esc_reader is None)):
                esc_reader = EscKeyReader(coord)
                _router, reply_input_fn = _build_interactive_input(
                    change, status, coord, esc_reader, driver)
                await tg.start(esc_reader.run)
                reader_armed = bool(esc_reader._armed)

            at_completion = final_state.completion_pending
            try:
                line, slash = await _collect_resume_input(
                    reply_input_fn, coord, at_completion=at_completion)
            except EOFError:
                # Ctrl-D at the resume prompt → pause-and-exit (rc 130), the same
                # finalize as a Ctrl-D during the run (plan §2: the interrupted
                # prompt's Ctrl-D pauses, it does not crash).
                return _finalize_paused_exit(change, driver, coord, "ctrl-d")
            if slash != "continue" and line.strip():
                coord.enqueue_interject(line)
            change.event_log().append(
                agent_id=None, type="workflow_resumed", payload={})
            running_state = State.load(change.state_dir)
            running_state.status = "running"
            running_state.save(change.state_dir)
        driver.finish()
        return _handle_terminal_outcome(change, result, coord)
    finally:
        if channel is not None:
            with anyio.CancelScope(shield=True):
                await channel.stop()
        status.reset()
        tg.cancel_scope.cancel()


def _finalize_paused_exit(
    change: Change, driver: Any, coord: Any, reason: str,
) -> int:
    """Finish the driver, persist the paused state (suppressing a duplicate
    workflow_paused when the engine already emitted one), journal the pause, and
    return rc 130. The single exit sequence shared by every interrupt/EOF/
    SIGTERM/escalation path out of the session loop."""
    driver.finish()
    pause_and_exit(change, reason=reason, engine_pause_reason=coord.pause_reason)
    _emit_paused(change, reason)
    return 130


async def _collect_resume_input(
    reply_input_fn: Callable[[], Awaitable[str]],
    coord: Any,
    *,
    at_completion: bool,
) -> tuple[str, str | None]:
    """Inner prompt loop for a paused interrupt round: re-prompt WITHOUT
    re-entering run_change until the user submits something that resumes. Returns
    (line, slash) at the resume decision. A bare Enter / empty-queue re-prompts;
    at completion only /continue (or Ctrl-D) finishes. Ctrl-D raises EOFError out
    of reply_input_fn; the caller wraps this call in `except EOFError` to
    pause-and-exit (rc 130) — EOFError is propagated, never swallowed here."""
    reminder_shown = False
    while True:
        line = await reply_input_fn()  # Ctrl-D → EOFError (propagated to caller)
        slash = "continue" if line.strip() == "/continue" else None
        if at_completion:
            if slash == "continue":
                return line, slash
            if sys.stdout.isatty():
                print("no step left to take input — "
                      "/continue to finish or ctrl-d to exit"
                      if line.strip()
                      else "/continue to finish or ctrl-d to exit")
            continue
        queued = coord.has_queued_interject()
        if _decide_resume_action(line=line, queued=queued, slash=slash) == "reprompt":
            if not reminder_shown and sys.stdout.isatty():
                reminder_shown = True
                print("enter a message, /continue to resume, "
                      "ctrl-d to exit")
            continue
        return line, slash


def _decide_resume_action(*, line: str, queued: bool, slash: str | None) -> str:
    """Pure §2 resume-vs-re-prompt decision. Returns 'resume' or 'reprompt'.

    Resume whenever there is text to deliver: a non-empty line, a queued
    interject, or explicit /continue. A bare Enter with an empty queue
    re-prompts (an accidental Enter must not relaunch a billable turn)."""
    if slash == "continue":
        return "resume"
    if line.strip():
        return "resume"
    if queued:
        return "resume"
    return "reprompt"


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

    block = _preflight_imp_init(control_root=control, project_root=control)
    if block is not None:
        print(f"imp: {block}", file=sys.stderr)
        return 2

    change_id = new_change_id(date=datetime.now(UTC).strftime("%Y-%m-%d"))
    # No auto-name: an unnamed Change has an empty title (slug == ""). The agent
    # names it via rename_change once intent is clear, or the user passes --title.
    title = args.title if args.title is not None else ""
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
            channel = _maybe_build_channel(change)
            rc = 0
            esc_reader: EscKeyReader | None = None
            if non_interactive:
                reply_input_fn: Callable[[], Awaitable[str]] = make_message_reply_fn(
                    args.message)
            else:
                esc_reader = EscKeyReader(coord)
                # Task 11: the ESC reader (sole /dev/tty owner during thinking)
                # also captures interject lines; the solicit/reply prompt is the
                # router-flagged reply_input_fn. See _build_interactive_input.
                _router, reply_input_fn = _build_interactive_input(
                    change, status, coord, esc_reader, driver)

            try:
                async with anyio.create_task_group() as tg:
                    tg.start_soon(coord.run_signal_receiver)
                    if not non_interactive:
                        tg.start_soon(status.tick)
                        assert esc_reader is not None
                        # Block until the reader has armed (or given up): a
                        # workflow whose first step is interactive must not start
                        # its prompt before the ESC reader holds /dev/tty, or the
                        # prompt and the reader race for the terminal's termios.
                        await tg.start(esc_reader.run)
                    rc = await _run_change_session(
                        change, coord=coord, driver=driver, status=status,
                        esc_reader=esc_reader, reply_input_fn=reply_input_fn,
                        non_interactive=non_interactive, tg=tg, channel=channel)
            except* (LoadError, LoadErrors) as eg:
                # A bad workflow/config raised on the FIRST run_change (it never
                # reaches the loop). _run_change_session has already cleaned up
                # (status.reset) in its finally; surface the error as rc 2.
                driver.finish()
                print(f"imp: {eg.exceptions[0]}", file=sys.stderr)
                rc = 2
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
                from imp.providers.effective import load_effective_config
                try:
                    cfg = load_effective_config(
                        project_root=change.project_root,
                        control_root=change.control_root,
                    ).config
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
            channel = _maybe_build_channel(change)
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

            esc_reader: EscKeyReader | None = None
            if non_interactive:
                reply_input_fn: Callable[[], Awaitable[str]] = make_message_reply_fn(
                    args.message)
            else:
                esc_reader = EscKeyReader(coord)
                # Task 11: ESC reader captures interjects; router-flagged prompt is
                # the reply channel (see _build_interactive_input).
                _router, reply_input_fn = _build_interactive_input(
                    change, status, coord, esc_reader, driver)

            try:
                async with anyio.create_task_group() as tg:
                    tg.start_soon(coord.run_signal_receiver)
                    if not non_interactive:
                        tg.start_soon(status.tick)
                        assert esc_reader is not None
                        # Block until the reader has armed (or given up): a
                        # workflow whose first step is interactive must not start
                        # its prompt before the ESC reader holds /dev/tty, or the
                        # prompt and the reader race for the terminal's termios.
                        await tg.start(esc_reader.run)
                    rc = await _run_change_session(
                        change, coord=coord, driver=driver, status=status,
                        esc_reader=esc_reader, reply_input_fn=reply_input_fn,
                        non_interactive=non_interactive, tg=tg, channel=channel)
            except* (LoadError, LoadErrors) as eg:
                # A bad workflow/config raised on the FIRST run_change (it never
                # reaches the loop). _run_change_session has already cleaned up
                # (status.reset) in its finally; surface the error as rc 2.
                driver.finish()
                print(f"imp: {eg.exceptions[0]}", file=sys.stderr)
                rc = 2
            return rc
    except LockHeld as e:
        print(f"imp: Change {change.id} is already running (pid {e.held_by_pid})",
              file=sys.stderr)
        return 2


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


# Row = (id, status, step, last_activity, title, created_at). created_at is the
# trailing sort key only — the renderer never displays it (see _render_status_table).
_Row = tuple[str, str, str, str, str, str]


def _status_row(change_id: str, state_json: bytes, change_json: bytes,
                last_activity: str) -> _Row | None:
    """Pure (id, status, step, last_activity, title, created_at) extractor, fed
    by either worktree file bytes or journal_show bytes. The journaled
    state.json and change.json are schema-identical to the in-flight ones (both
    written by State.save / Change.create), so the two sections can't silently
    drift. Returns None on malformed/incomplete JSON so a single corrupt entry
    can't crash the whole `imp status` listing (callers skip None)."""
    try:
        st = json.loads(state_json)
        meta = json.loads(change_json)
        return (change_id, st["status"], st.get("step") or "-", last_activity,
                meta["title"], meta["created_at"])
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def _status_rows(control: Path) -> list[_Row]:
    rows: list[_Row] = []
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


def _completed_rows(control: Path) -> list[_Row]:
    """Journal-backed completed Changes: every journaled id NOT present as a
    live worktree (in-flight takes precedence on the crash-window overlap)."""
    live = set(_list_change_worktrees(control))
    rows: list[_Row] = []
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
    "abandoned": ("gray",),
}


def _render_status_table(
    rows: list[_Row],
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
    for slug, status, step, last, title, _created in rows:
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
    # In-flight Changes show by default; completed/abandoned ones are journaled
    # history and only listed with --all (they'd otherwise pile up indefinitely).
    rows = _status_rows(control)
    if args.all:
        rows = rows + _completed_rows(control)
    if not rows:
        print("No Changes.")
        return 0
    # Sort by last activity, oldest first. last_activity (r[3]) is a fixed-width
    # "YYYY-MM-DD HH:MM:SS" local-time string (or "-" when there's no event), so
    # lexicographic ascending == chronological oldest-first; activity-less "-"
    # rows ("-" < any digit) sort to the top. created_at (r[5]) breaks ties
    # deterministically (older Change first within the same activity second).
    rows.sort(key=lambda r: (r[3], r[5]))
    color_enabled = should_use_color(sys.stdout)
    for line in _render_status_table(rows, color_enabled=color_enabled):
        print(line)
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
        render_config(eff, sys.stdout, color=should_use_color(sys.stdout))
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
                       help="Headless: no live prompt; supply the reply via --message.")
    p_new.add_argument("--message", metavar="TEXT", default=None,
                       help="Headless reply consumed by the first wait_for_user; "
                            "pause+resume with another --message. "
                            "Requires --non-interactive.")
    p_new.set_defaults(func=_cmd_new)

    p_resume = sub.add_parser("resume")
    p_resume.add_argument("token")
    p_resume.add_argument("--non-interactive", action="store_true",
                          help="Headless: no live prompt; supply the reply via --message.")
    p_resume.add_argument("--message", metavar="TEXT", default=None,
                          help="Headless reply consumed by the first wait_for_user; "
                               "pause+resume with another --message. "
                               "Requires --non-interactive.")
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
    p_status.add_argument(
        "-a", "--all", action="store_true",
        help="Also list completed and abandoned Changes (default: in-flight only).",
    )
    p_status.set_defaults(func=_cmd_status)

    p_config = sub.add_parser("config")
    p_config.add_argument("--explain", metavar="TIER", default=None,
                          help="trace one tier's bindings through the layers")
    p_config.set_defaults(func=_cmd_config)

    p_abandon = sub.add_parser(
        "abandon",
        help="Tear down a Change's worktree, branch, and state (logged).",
    )
    p_abandon.add_argument("token")
    p_abandon.set_defaults(func=_cmd_abandon)

    from imp.evals.cli import add_eval_parser

    add_eval_parser(sub)

    ns = parser.parse_args(args)
    _apply_theme()   # resolve dark/light/ansi once, before any command renders
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
