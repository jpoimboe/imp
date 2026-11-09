# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
from __future__ import annotations

import contextlib
import sys
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from typing import Any

import anyio

from imp.cli.abandon_confirm import ask_keypress_confirm
from imp.cli.exit_codes import ExitCode
from imp.cli.palette import should_use_color
from imp.cli.prompt import Prompt
from imp.cli.session_terminal import RunWitness, run_terminal_client
from imp.cli.signals import InterruptCoordinator
from imp.cli.slash import ask_line_with_slash
from imp.cli.terminal_title import TerminalTitle
from imp.cli.terminal_watchdog import WatchedStream
from imp.engine.quest import Quest
from imp.engine.state import State
from imp.remote.session.attach import (
    LAYOUT_MISMATCH_EXIT,
    DaemonStartupError,
    build_session_attach,
)
from imp.remote.session.client import StreamEnding
from imp.remote.session.protocol import SkillRequest


def _should_run_ctrl_d_reader(*, non_interactive: bool) -> bool:
    """Run the CLI Ctrl-D reader only for an interactive tty run (never headless/piped)."""
    return not non_interactive and sys.stdin.isatty()


def build_provider_picker(
    quest: Quest, driver: Any, status: Any,
) -> Callable[[], contextlib.AbstractContextManager[list[str | None]] | None]:
    """The picker's rendering seam: CtrlDReader has no output channel, so the
    block is drawn here, where driver and quest are both in scope. The suspend
    bracket spans the digit read so the 100ms footer tick cannot paint over it;
    tty_quiesced would be wrong — the reader still owns the tty. None outside a
    retry backoff: `p` opens in exactly the window `r` is honored in."""
    from imp.cli.provider_command import (
        current_provider_for,
        picker_choices,
        quest_provider_groups,
        render_picker,
    )
    from imp.engine.errors import LoadError, LoadErrors
    from imp.providers import ProviderGroups

    @contextlib.contextmanager
    def _block(groups: ProviderGroups) -> Iterator[list[str | None]]:
        current = current_provider_for(quest)
        suspend = (status.suspend() if status is not None
                   else contextlib.nullcontext())
        with suspend:
            driver.emit_lines(render_picker(current, groups))
            yield picker_choices(current, groups)

    def _open() -> contextlib.AbstractContextManager[list[str | None]] | None:
        if not driver.retry_countdown_in_flight():
            return None
        try:
            groups = quest_provider_groups(quest)
        except (LoadError, LoadErrors) as exc:
            suspend = (status.suspend() if status is not None
                       else contextlib.nullcontext())
            with suspend:
                driver.emit_lines(str(exc).splitlines())
            return None
        return _block(groups)

    return _open


@dataclass(frozen=True)
class SettledOutcome:
    """Resolved terminal state; status is None when neither home nor archive can answer."""

    status: str | None
    events: list[dict[str, Any]]
    merge_commit: str | None = None

    @property
    def completed(self) -> bool:
        return self.status == "completed"


def resolve_settled_outcome(quest: Quest) -> SettledOutcome:
    """Read status and events from one source: the live home if state.json loads, else
    the archive — a reaped home can still hold a stray events.jsonl line. An archive
    this install cannot read counts as absent, so a teardown never dies on skew."""
    from imp.cli.store import _parse_jsonl
    from imp.engine.archive import read as archive_read
    from imp.engine.errors import ArchiveMismatch
    try:
        state = State.load(quest.state_dir)
    except FileNotFoundError:
        try:
            raw = archive_read.read_events(quest.control_root, quest.id)
            status = archive_read.status_of(quest.control_root, quest.id)
            merge_commit = archive_read.merge_commit_of(quest.control_root, quest.id)
        except ArchiveMismatch:
            return SettledOutcome(status=None, events=[])
        return SettledOutcome(
            status=status, events=_parse_jsonl(raw) if raw is not None else [],
            merge_commit=merge_commit)
    return SettledOutcome(status=state.status, events=list(quest.event_log().read()),
                          merge_commit=state.merge_commit)


def _emit_settled_completion(
    quest: Quest, outcome: SettledOutcome, *, intent: str | None,
) -> None:
    """The completed block from a disk-resolved outcome: merged form when the record
    carries a merge commit, with the commits recorded on that ending."""
    from imp.cli.main import Merge, _emit_completed, _recorded_commits
    merge = (Merge(base=quest.base_branch, commit=outcome.merge_commit)
             if outcome.merge_commit is not None else None)
    _emit_completed(
        quest,
        merge=merge,
        intent=intent,
        commits=_recorded_commits(outcome.events, merged=merge is not None))


def _closing_block(
    quest: Quest, *, witness: RunWitness, ending: StreamEnding,
    coord: InterruptCoordinator, leave_state: dict[str, str],
) -> ExitCode:
    """Select the closing block from what this client witnessed, in row order: its own
    leave, then a leave the end frame named, then the run's own outcome. Two rows
    consult disk, and only to upgrade to a completed run, never to invent a pause:
    the deliberate-leave row, since the daemon may have completed before it
    processed the leave, and the last row, where the stream broke before anything
    could be witnessed."""
    from imp.cli.main import (
        Merge,
        _emit_completed,
        _emit_daemon_died,
        _emit_leave_block,
        _emit_leave_pause,
        _emit_lost_connection,
        _emit_merge_blocked,
        _emit_paused_summary,
        _leave_trigger,
        _print_leave_disposition,
    )
    from imp.cli.store import _resolve_intent
    intent = witness.intent()
    trigger = _leave_trigger(coord)
    disposition_rc = _print_leave_disposition(quest, leave_state, trigger=trigger,
                                              intent=intent)
    if disposition_rc is not None:
        return disposition_rc
    leave = witness.leave()
    if leave is not None:
        _emit_leave_block(quest, leave, intent=intent)
        return ExitCode.USER_ENDED
    terminal = witness.terminal()
    if terminal is not None:
        if terminal.type == "quest_merged":
            _emit_completed(
                quest,
                merge=Merge(base=terminal.payload["base"], commit=terminal.payload["commit"]),
                intent=intent,
                commits=terminal.payload.get("commits", []))
            return ExitCode.OK
        if terminal.type == "workflow_completed":
            _emit_completed(quest, merge=None, intent=intent,
                            commits=terminal.payload.get("commits", []))
            return ExitCode.OK
        _emit_merge_blocked(
            quest, terminal.payload.get("reason"), terminal.payload.get("detail"),
            terminal.payload.get("note"), intent=intent)
        return ExitCode.FAILED
    if trigger is not None:
        # A deliberate leave outranks a witnessed pause, but never a completion seen
        # on the stream or on disk.
        settled = resolve_settled_outcome(quest)
        if settled.completed:
            _emit_settled_completion(
                quest, settled, intent=_resolve_intent(settled.events) or intent)
            return ExitCode.OK
        _emit_leave_pause(quest, trigger, intent=intent)
        return ExitCode.USER_ENDED
    paused = witness.paused()
    if paused is not None:
        _emit_paused_summary(quest, info=paused.info, intent=intent)
        return ExitCode.FAILED if paused.failed else ExitCode.USER_ENDED
    if coord.escalation_event is not None and coord.escalation_event.is_set():
        _emit_paused_summary(quest, info=None, intent=intent)
        return ExitCode.USER_ENDED
    if ending is StreamEnding.ENDED:
        # An end frame arrived, so the ordering guarantee holds: an outcome the client
        # failed to witness is structurally impossible. The run reported nothing.
        _emit_daemon_died(quest, intent=intent)
        return ExitCode.FAILED
    outcome = resolve_settled_outcome(quest)
    settled_intent = _resolve_intent(outcome.events)
    if outcome.completed:
        _emit_settled_completion(quest, outcome, intent=settled_intent)
        return ExitCode.OK
    _emit_lost_connection(quest, intent=settled_intent or intent)
    return ExitCode.LOST_CONNECTION


async def run_session_client(
    quest: Quest,
    *,
    driver: Any,
    coord: InterruptCoordinator,
    solicit: Callable[[str, bool], Awaitable[str | SkillRequest]],
    confirm: Callable[[str], Awaitable[bool]],
    status: Any = None,
    resume: bool = False,
    leave_mode: str,
    leave_state: dict[str, str] | None = None,
    title: TerminalTitle | None = None,
    stream: WatchedStream,
    non_interactive: bool = False,
    autonomous: bool = False,
    model: str | None = None,
    provider: str | None = None,
    opening_unsandboxed: bool = False,
) -> ExitCode:
    """Attach to the session daemon, render events, answer gates, and return the exit code."""
    leave_state = {} if leave_state is None else leave_state
    title = title if title is not None else TerminalTitle(
        stream,
        quest.id, quest.title, project=quest.control_root.name)
    attach = build_session_attach(
        quest, resume=resume,
        non_interactive=non_interactive, autonomous=autonomous, lease=True,
        model=model, opening_unsandboxed=opening_unsandboxed)
    startup_failed = False
    startup_error: DaemonStartupError | None = None
    ending = StreamEnding.CUT
    witness = RunWitness()
    async with anyio.create_task_group() as tg:
        tg.start_soon(coord.run_signal_receiver)
        if _should_run_ctrl_d_reader(non_interactive=non_interactive):
            from imp.cli.signals import CtrlDReader
            await tg.start(CtrlDReader(
                coord,
                provider_picker=build_provider_picker(
                    quest, driver, status)).run)
        if not non_interactive:
            # Interactive tty only; headless never registers it so the ack no-ops.
            coord.set_ack_hook(driver.show_stop_ack)
        if status is not None:
            tg.start_soon(status.tick)
        driver.install_startup_status()
        try:
            ending, witness = await run_terminal_client(
                attach=attach, driver=driver, coord=coord, solicit=solicit, confirm=confirm,
                quest_id=quest.id, control_root=quest.control_root,
                leave_state=leave_state, leave_mode=leave_mode, title=title,
                stream=stream, provider=provider,
                local_echo=driver.seed_pending_reply,
                on_title=lambda new: setattr(quest, "title", new))
        except TimeoutError:
            startup_failed = True
            # attach raised before run_terminal_client's teardown finally runs.
            title.reset()
        except DaemonStartupError as err:
            startup_failed = True
            startup_error = err
            title.reset()
        tg.cancel_scope.cancel()
    if startup_failed and not resolve_settled_outcome(quest).completed:
        if startup_error is not None and startup_error.exit_code == LAYOUT_MISMATCH_EXIT:
            print(startup_error.stderr_tail, file=sys.stderr)
            return ExitCode.USAGE
        from imp.cli.main import _emit_startup_failed
        _emit_startup_failed(quest, startup_error)
        return ExitCode.STARTUP_FAILED
    return _closing_block(
        quest, witness=witness, ending=ending, coord=coord, leave_state=leave_state)


def make_headless_solicit(
    message: str | None, coord: InterruptCoordinator,
) -> Callable[[str, bool], Awaitable[str]]:
    """Headless solicit: deliver `message` at the first gate when that gate is a bare
    turn-end yield, then signal a clean detach and block. The latch is spent at the first
    gate whatever it is, so an interrupt gate discards the message — a line meant for one
    question can never answer another — and falls through to the detach."""
    spent = False

    async def _solicit(_prompt: str, interrupt: bool = False) -> str:
        nonlocal spent
        if message is not None and not spent:
            spent = True
            if not interrupt:
                return message
        # Trip the escalation event so run_terminal_client tears the client down
        # (run survives, the daemon hibernates at the gate); then block — never
        # returning so no stray reply is sent as an input frame.
        coord.escalation_event.set()
        await anyio.sleep_forever()
        raise AssertionError("unreachable")  # anyio 4.8: sleep_forever is -> None, not NoReturn

    return _solicit


def make_interactive_solicit(
    quest: Quest,
    *,
    status: Any,
    driver: Any,
    coord: InterruptCoordinator,
) -> Callable[[str, bool], Awaitable[str | SkillRequest]]:
    """Return a solicit callback that reads one slash-aware line from the tty as the gate reply."""
    # Lazy import: imp.cli.main imports this module, so a
    # module-level import of these helpers would be circular.
    from imp.cli.main import _editing_mode, _prompt_history_lines, _term_width

    prompt = Prompt(
        skills_dir=quest.workflow_dir / "skills",
        editing_mode=_editing_mode(quest.control_root),
        color=should_use_color(sys.stdout), quest=quest,
        output_fn=driver.commit_row)
    prompt.seed_history(
        _prompt_history_lines(quest.seed_opening, quest.event_log().read()))

    async def _solicit(_prompt: str, interrupt: bool = False) -> str | SkillRequest:
        # The gate's prompt (the interrupt-steer hint, or the daemon's resume
        # prompt) surfaces as the field placeholder; an empty prompt — the bare
        # turn-end yield — falls back to the shared DEFAULT_HINT placeholder.
        # An interrupt gate allows an empty line to return "" (continue).
        return await ask_line_with_slash(
            prompt, status, quest,
            color=should_use_color(sys.stdout), width=_term_width(),
            coord=coord, driver=driver, hint=_prompt or None,
            allow_empty=interrupt)

    return _solicit


def make_interactive_confirm(
    *, status: Any, driver: Any, coord: InterruptCoordinator,
) -> Callable[[str], Awaitable[bool]]:
    """The session's single-keypress confirm, framed like a solicit turn so the
    status footer, the Ctrl-D reader and SIGINT routing yield the tty to it."""

    async def _confirm(question: str) -> bool:
        coord.set_at_prompt(True)
        try:
            async with coord.tty_quiesced():
                with status.suspend():
                    driver.prepare_for_prompt()
                    try:
                        return await ask_keypress_confirm(question, driver.commit_row)
                    finally:
                        driver.flush_deferred()
        finally:
            coord.set_at_prompt(False)
            coord.rearm_signal_handlers()

    return _confirm


async def confirm_unasked(_question: str) -> bool:
    """A headless session has no one to ask: a delivered /abandon proceeds."""
    return True
