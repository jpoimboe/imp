"""imp CLI entrypoint.

Plan 1: minimal commands (new, resume, log, status). No streaming, no signals,
no spinner. Plan 3 adds the polished UX layer.
"""

from __future__ import annotations

import argparse
import contextlib
import enum
import functools
import io
import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple, NoReturn

import anyio

from imp import __version__
from imp.agents.random_status import random_status_phrase
from imp.agents.skills import load_user_skill
from imp.cli import palette as pal
from imp.cli import quest_row, resume_select, splash
from imp.cli.inert_resume import (
    bare_turn_end_question,
    make_buffered_solicit,
    render_inert_question,
    run_inert_resume,
)
from imp.cli.init import GitSetupDeclined, IntegrationBranchUnresolved, run_init
from imp.cli.live_driver import LiveDriver, compose_prelaunch_position_row
from imp.cli.live_status import LiveStatus
from imp.cli.palette import ansi, should_use_color
from imp.cli.prompt import DEFAULT_HINT, Prompt
from imp.cli.session_run import (
    infer_session_result,
    make_headless_solicit,
    make_interactive_solicit,
    run_session_client,
)
from imp.cli.signals import install_process_group, prompt_sigint_guard
from imp.cli.slash import ask_line_with_slash
from imp.cli.store import HomeDigest, SearchHit, SearchZones, Store, make_store
from imp.cli.terminal_title import TerminalTitle
from imp.diagnostics import install_stack_dumper
from imp.engine import backlog, worktree
from imp.engine.abandon import (
    discard_conversation,
    tear_down_quest,
)
from imp.engine.archive import read as archive_read
from imp.engine.awaiting_user_input import AwaitingUserInput
from imp.engine.errors import LayoutMismatch, LoadError, LoadErrors
from imp.engine.executor import RunResult
from imp.engine.project_root import find_project_root
from imp.engine.quest import Quest, live_quest_ids, live_stems
from imp.engine.quest_id import new_quest_id
from imp.engine.state import LockHeld, State, lock_is_live
from imp.providers.config import (
    parse_default_base,
    parse_default_workflow,
    validate_workflow_name,
)
from imp.remote.daemon.directory import session_is_resident
from imp.remote.session.protocol import SkillRequest


class ReplyInterjectRouter:
    """Routes each human-typed line by the agent's state AT SUBMIT TIME (F7/R6).

    `route(line)` is the pure decision:
      - solicit ACTIVE (the engine is blocked at a bare-turn-end prompt)
        → the line is the REPLY → delivered to `reply_sink`;
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
    coupling the quest/event-log and a status-suspending print into the raw-tty
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


def _print_splash_banner(
    *, interactive: bool, lead_blank: bool = True, stream: Any = None,
) -> bool:
    """Optional leading blank line then the splash scene, printed on an interactive
    entry only. Shared by new/chat and resume so the banner is identical everywhere.
    `lead_blank=False` skips the leading blank for callers whose header already
    emitted one. Returns True only when the banner was actually printed (interactive
    and a non-empty scene), so the opening flow can tie its separator to it."""
    if not interactive:
        return False
    out = sys.stdout if stream is None else stream
    block = splash.splash_block(theme=pal.active_theme(), width=_term_width(), color=True)
    if not block:
        return False
    if lead_blank:
        print(file=out)
    print(block, file=out)
    print(file=out)
    return True


class SeedError(Exception):
    """A bad seed source: e.g. a misused flag combination."""


class SeedResolution(NamedTuple):
    title: str | None
    seed_text: str | None


def resolve_spec_seed(args: argparse.Namespace) -> SeedResolution:
    """Resolve the first-turn seed source for `imp new`.

    A non-empty positional MESSAGE becomes the user's verbatim opening intent
    (delivered unwrapped); otherwise there is no seed. A spec is named in the
    positional MESSAGE and read by the workflow's opening step itself, never inlined here."""
    title = getattr(args, "title", None)
    opening = getattr(args, "opening", None)

    if opening is not None and opening.strip():
        return SeedResolution(title, opening)

    return SeedResolution(title, None)


def make_message_reply_fn(message: str | None) -> Callable[[], Awaitable[str]]:
    """Headless reply channel: yield the single --message once, then pause.

    The first bare-turn-end solicit (live opener OR the awaiting_user_input
    resume) consumes `message`; any later solicit in the same
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
# Every terminal line shares one spine: a gold quest id, a middle display-name
# segment, and a dot-joined outcome tail whose color carries meaning (green =
# success, red = error, gold = everything-else). The middle segment is always
# present via the shared resolver (quest_row.resolve_display_name): a real title
# is plain, else the opening prompt renders dim with a plain-ASCII `> ` prefix,
# else the dim `(unnamed)` placeholder. Exception to the old "no dim, no glyphs"
# rule — but dim is color-only and `> ` is plain ASCII, so NO_COLOR / non-TTY
# output stays plain and script-parseable. Shared by `new`/`resume` and leaves.

def _spine_name(title: str, intent: str | None, *,
                color: bool) -> tuple[str, quest_row.NameKind]:
    """The styled middle name segment and its kind, resolved title→intent→(unnamed)."""
    resolved = quest_row.resolve_display_name(title, intent)
    if resolved.kind is quest_row.NameKind.PROMPT:
        name = quest_row.PROMPT_PREFIX + quest_row._truncate(
            resolved.text, quest_row.NAME_CAP - len(quest_row.PROMPT_PREFIX))
    else:
        name = quest_row._truncate(resolved.text, quest_row.NAME_CAP)
    if resolved.kind is not quest_row.NameKind.TITLE:
        name = pal.ansi(name, "dim", enabled=color)
    return name, resolved.kind


def _status_spine(quest_id: str, title: str, tail: str, *tail_codes: str,
                  color: bool, intent: str | None = None) -> str:
    """`<id>gold · <name> · <tail>colored`; the middle segment is always present."""
    name, _ = _spine_name(title, intent, color=color)
    return (ansi(quest_id, *pal.NODE_COLORS["quest_id"], enabled=color)
            + f" · {name} · " + ansi(tail, *tail_codes, enabled=color))


def _resume_line(token: str, suffix: str = "") -> str:
    """A plain (default-fg), flush-left resume hint; one space after `resume:`."""
    return f"resume: imp resume {token}{suffix}"


def _print_warnings_block(messages: list[str]) -> None:
    """A plain `N warnings this run` header + a plain dashed list of the distinct
    messages in first-seen order, `(×N)` on repeats. No-op on an empty list."""
    if not messages:
        return
    counts: dict[str, int] = {}
    order: list[str] = []
    for msg in messages:
        if msg not in counts:
            counts[msg] = 0
            order.append(msg)
        counts[msg] += 1
    print()
    noun = "warning" if len(messages) == 1 else "warnings"
    print(f"{len(messages)} {noun} this run")
    for msg in order:
        suffix = f" (×{counts[msg]})" if counts[msg] > 1 else ""
        print(f"- {msg}{suffix}")


def _emit_completed(quest: Quest, *, merged: bool,
                    warnings: list[str] | None = None,
                    intent: str | None = None) -> None:
    """The clean-finish line: one green-tailed spine, then the warnings block."""
    color = should_use_color(sys.stdout)
    tail = f"merged into {quest.base_branch}" if merged else "completed"
    print(_status_spine(quest.id, quest.title, tail, "green", color=color,
                        intent=intent))
    _print_warnings_block(warnings or [])
    print()


class _PauseBanner(enum.Enum):
    """Sentinel: latest segment ended without a pause; its reason is stale."""
    STALE = enum.auto()


# Single source: error pause-reason base token -> user-facing label; covers all _POLICY.
_ERROR_LABELS: dict[str, str] = {
    "terminated": "agent error",
    "internal_error": "internal error",
    "api": "API error",
    "quota": "quota exceeded",
    "context_window": "context limit",
    "config": "config error",
    "content_filter": "content filtered",
    "loop": "loop detected",
    "unknown": "unknown error",
    "retry_ceiling": "retries exhausted",
    "reauth_needed": "auth failed",
    "auth": "auth failed",
    "rate_limit": "rate limited",
    "network": "network error",
    "stalled": "stalled",
    "agent_output_missing": "no output",
    "crash": "crash",
}


def _error_block(head: str, body: list[str], quest_id: str, title: str,
                 *, color: bool, intent: str | None = None) -> list[str]:
    """The shared three-part error block: a red `head` line, `body` lines in the
    default fg, then the identity line (gold id, ` · `, then the resolved name —
    the title, else the dim opening-prompt fallback; a bare gold id only when the
    quest has neither). No resume line."""
    lines = [ansi(head, "red", enabled=color)]
    lines.extend(body)
    ident = ansi(quest_id, *pal.NODE_COLORS["quest_id"], enabled=color)
    name, kind = _spine_name(title, intent, color=color)
    if kind is not quest_row.NameKind.UNNAMED:
        ident = f"{ident} · {name}"
    lines.append(ident)
    return lines


@dataclass(frozen=True)
class _PauseInfo:
    """A terminal pause's structured reason: the base token (`api`, `terminated`,
    `interrupt`, a deliberate-leave trigger, `crash`, …) and the full cause text
    (multi-line allowed), or None when the reason carries no cause."""
    base: str
    cause: str | None


def _pause_reason(events: list[dict[str, Any]]) -> _PauseInfo | _PauseBanner | None:
    """The terminal pause's structured reason (a _PauseInfo) for the headline.

    The last workflow_paused reason (interrupt_reason wins, mirroring
    _h_workflow_paused), plus the full cause text from the error that drove that
    pause (the error's detail.last_agent_text, else its message — the full cause
    text, multi-line allowed; the renderer splits its first line as the red
    summary from the remaining body lines). The event log is cumulative across
    resume, so only an error with no intervening step_started before the pause
    contributes a cause — a recovered transient error followed by a step_started
    is dropped, and only the causal error (same invocation, no step start after
    it) reaches the pause. An interrupt pause with no error in its segment
    renders the bare reason, even when an earlier segment terminated. Returns
    _PauseBanner.STALE when the latest segment ended without its own
    segment-terminal event — a resume with no workflow_paused (or, for a
    draft, conversation_saved) after it — so the stored reason is a fossil;
    _pause_facts then classifies it — the unexpected-death banner only when
    daemon.stderr holds a crash traceback (e.g. a workflow LoadError at daemon
    startup), otherwise a calm interrupt. A resumed draft that never paused
    before parking has no workflow_paused at all; the resume alone is enough to
    arm the check. Builds only from existing event fields — no hardcoded kind."""
    paused: dict[str, Any] | None = None
    paused_error: dict[str, Any] | None = None
    pending_error: dict[str, Any] | None = None
    resume_pending = False
    for e in events:
        t = e.get("type")
        if t == "workflow_paused":
            paused = e.get("payload") or {}
            paused_error = pending_error
            pending_error = None
            resume_pending = False
        elif t == "workflow_resumed":
            resume_pending = True
        elif t == "conversation_saved":
            resume_pending = False
        elif t == "step_started":
            pending_error = None
        elif t == "error":
            pending_error = e.get("payload") or {}
    if resume_pending:
        return _PauseBanner.STALE
    if paused is None:
        return None
    base = paused.get("interrupt_reason") or paused.get("reason")
    if not base:
        return None
    # A genuine your-turn park is a normal solicit, not an interrupt: surface no
    # reason, so the trailing status line shows the bare `paused` tail rather than
    # a reason. Scoped to awaiting_user; STALE / error-cause paths untouched.
    if base == "awaiting_user":
        return None
    cause: str | None = None
    if paused_error is not None:
        d = paused_error.get("detail")
        if isinstance(d, dict) and d.get("last_agent_text"):
            cause = str(d["last_agent_text"]).strip() or None
        if cause is None and paused_error.get("message"):
            cause = str(paused_error["message"]).strip() or None
    return _PauseInfo(base=str(base), cause=cause)


def _pause_is_error(info: _PauseInfo | None) -> bool:
    """A pause is an error (red block) exactly when its base token is a key in the
    label map. Everything else — interrupt, deliberate-leave triggers, a your-turn
    `paused`, and any other current gold reason (e.g. `script_blocked`) — is not."""
    return info is not None and info.base in _ERROR_LABELS


def _pause_lines(
    name: str, token: str,
    *, info: _PauseInfo | None = None, title: str = "", color: bool,
    intent: str | None = None,
) -> list[str]:
    if _pause_is_error(info):
        assert info is not None
        label = _ERROR_LABELS[info.base]
        head, body = label, []
        if info.cause:
            first, *rest = info.cause.splitlines()
            first = first.strip()
            head = f"{label}: {first}" if first else label
            body = rest
        return _error_block(head, body, name, title, color=color, intent=intent)
    tail = info.base if info is not None else "paused"
    return [
        _status_spine(name, title, tail, "wordmark", color=color, intent=intent),
        _resume_line(token),
    ]


def _leave_trigger(coord: Any) -> str | None:
    """The deliberate-leave trigger string, else None."""
    if coord is None:
        return None
    evt = getattr(coord, "escalation_event", None)
    if evt is not None and evt.is_set() and coord.leave_reason is not None:
        reason: str | None = coord.leave_reason
        return reason
    return None


_DAEMON_CRASH_MARKER = "Traceback (most recent call last):"


def _daemon_stderr_has_crash(path: Path) -> bool:
    """True when daemon.stderr holds error-level content (a Python traceback).

    The session daemon configures no logging, so this file is raw process
    stderr: empty or benign provider INFO chatter on a clean detach, and a
    traceback on a genuine death (the executor's re-raise, or the interpreter's
    unhandled-exception printer). Only the traceback marker earns the
    unexpected-death banner — a non-empty file is not on its own a crash."""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return False
    return _DAEMON_CRASH_MARKER in text


def _pause_facts(
    quest: Quest, leave_trigger: str | None = None,
) -> tuple[str, str, str, _PauseInfo | None, str | None]:
    name = quest.id
    events = list(quest.event_log().read())
    opening = _resolve_intent(events)
    if leave_trigger is not None:
        # Deliberate leave: render the trigger locally (daemon reason may lag / read STALE).
        return name, quest.title, quest.id, _PauseInfo(leave_trigger, None), opening
    reason_val = _pause_reason(events)
    if reason_val is _PauseBanner.STALE:
        if _daemon_stderr_has_crash(quest.quest_dir / "daemon.stderr"):
            info: _PauseInfo | None = _PauseInfo(
                "crash", f"see .imp/quests/{quest.id}/daemon.stderr")
        else:
            # A resumed-without-pause segment with no crash traceback is a clean
            # deliberate detach — render the calm interrupt reason, no pointer.
            info = _PauseInfo("interrupt", None)
    else:
        info = reason_val
    return name, quest.title, quest.id, info, opening


def _emit_paused_summary(quest: Quest, leave_trigger: str | None = None) -> None:
    """The trailing run-status line on a non-interactive pause."""
    color = should_use_color(sys.stdout)
    name, title, token, info, opening = _pause_facts(quest, leave_trigger)
    for ln in _pause_lines(name, token, info=info, title=title, color=color,
                           intent=opening):
        print(ln)
    print()


def _emit_merge_blocked(
    quest: Quest, reason: str | None, detail: str | None = None,
    note: str | None = None,
) -> None:
    """Merge couldn't complete — the user must act. Renders the shared three-part
    error block: a red `merge blocked: <summary>` head, the per-reason instruction
    body (plus `note`, then git's own `git said: <detail>`) in the default fg, then
    the gold identity line (`id · title`, or the opening-prompt fallback when the
    quest has no title). No standalone resume line — the inline
    `imp resume <id>` in the body is the remediation.

    `detail` is git's own stderr (carried on the `merge_blocked` event); appended
    as `git said:` only for the neutral `refused` / `conflict` buckets, so the
    surfaced reason is truthful for every refusal, not just the overwrite one.
    `note` (e.g. the recovery-exhaustion note) renders before git's detail.
    `worktree_dirty` has a precise template and gets no detail."""
    color = should_use_color(sys.stdout)
    cid, base = quest.id, quest.base_branch
    if reason == "worktree_dirty":
        summary = "uncommitted work in the worktree"
        body = [f"Commit it, then `imp resume {cid}`."]
    elif reason == "refused":
        summary = f"git refused to merge imp/{cid} into {base}"
        body = [f"Commonly: uncommitted changes on `{base}` that the merge would "
                f"overwrite. Resolve, then `imp resume {cid}`."]
    elif reason == "conflict":
        summary = f"imp/{cid} doesn't auto-merge into {base}"
        body = [f"imp can't resolve conflicts in this version — merge `imp/{cid}` "
                f"into `{base}` manually, then the quest is done (its worktree at "
                f"`.imp/worktrees/{cid}` can be removed)."]
    else:
        summary = "couldn't merge"
        body = [f"Resolve, then `imp resume {cid}`."]
    if note:
        body.append(note)
    if detail and reason in ("refused", "conflict"):
        body.append(f"git said: {detail.strip()}")
    opening = _resolve_intent(list(quest.event_log().read()))
    for ln in _error_block(f"merge blocked: {summary}", body, cid, quest.title,
                           color=color, intent=opening):
        print(ln)
    print()


def _handle_terminal_outcome(
    quest: Quest, result: RunResult, coord: Any,
) -> int:
    """Render the terminal outcome of a settled run and return its rc.

    COMPLETED → green completion line (rc 0). PAUSED → merge_blocked exits early
    (no summary); every other pause falls through to the dim paused summary (rc 1).
    Shared by `new`/`resume` (relocated verbatim from the post-run `else` arm)."""
    if result is RunResult.COMPLETED:
        # Worktree gone = the merge gate was exercised.
        wt_path = quest.control_root / ".imp" / "worktrees" / quest.id
        _ep = getattr(quest, 'events_path', None)
        if _ep is not None and not _ep.exists():
            events: list[dict[str, Any]] = []
        else:
            events = list(quest.event_log().read())
        if not events:
            raw = archive_read.read_events(quest.control_root, quest.id)
            events = _parse_jsonl(raw) if raw is not None else []
        warning_msgs = [
            str((ev.get("payload") or {}).get("message", "?"))
            for ev in events if ev.get("type") == "runner_warning"]
        _emit_completed(quest, merged=not wt_path.exists(), warnings=warning_msgs,
                        intent=_resolve_intent(events))
        return 0
    # PAUSED — surface merge_blocked; every other pause falls through to the summary.
    final_state = State.load(quest.state_dir)
    if final_state.status == "merge_blocked":
        mb_reason: str | None = None
        mb_detail: str | None = None
        mb_note: str | None = None
        for ev in quest.event_log().read():
            if ev.get("type") == "merge_blocked":
                payload = ev.get("payload") or {}
                mb_reason = payload.get("reason")
                mb_detail = payload.get("detail")
                mb_note = payload.get("note")
        _emit_merge_blocked(quest, mb_reason, mb_detail, mb_note)
        return 1  # friendly block already carries the resume command; skip redundant summary
    trigger = _leave_trigger(coord)
    if trigger == "ctrl-d":
        print(pal.caret(enabled=should_use_color(sys.stdout)))
    _emit_paused_summary(quest, trigger)
    return 1


def _print_leave_disposition(
    quest: Quest, leave_state: dict[str, str],
) -> int | None:
    """Print the optimistic leave message for a daemon-applied disposition.

    The disposition itself is applied by the daemon after it releases the lock;
    here we only echo what was (or is being) done, on the shared spine. Returns 0
    when a leave was handled, None to fall through to the normal exit-code path.
    """
    color = should_use_color(sys.stdout)
    intent = leave_state.get("intent")
    opening = _resolve_intent(list(quest.event_log().read()))
    if intent == "abandon":
        print(_status_spine(quest.id, quest.title, "abandoned", "wordmark", color=color,
                            intent=opening))
        print()
        return 0
    disposition = leave_state.get("disposition")
    if disposition == "save":
        print(_status_spine(quest.id, quest.title, "kept as draft", "wordmark",
                            color=color, intent=opening))
        print(_resume_line(quest.id))
        print()
        return 0
    if disposition == "archive":
        print(_status_spine(quest.id, quest.title, "shelved", "wordmark",
                            color=color, intent=opening))
        print(_resume_line(quest.id, " — or find it later with imp search"))
        print()
        return 0
    if disposition == "discard":
        return 0
    if leave_state.get("send_failed"):
        print(_status_spine(quest.id, quest.title, "left running", "wordmark",
                            color=color, intent=opening))
        print(_resume_line(quest.id))
        print()
        return 0
    return None


def _session_exit_code(quest: Quest, coord: Any) -> int:
    result = infer_session_result(quest)
    outcome = _handle_terminal_outcome(quest, result, coord)
    if result is RunResult.PAUSED and coord.interrupted:
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


def _test_cmd_fast_nudge(
    *, project_root: Path, control_root: Path, non_interactive: bool,
    input_fn: Callable[[str], str], output_fn: Callable[[str], None],
    err_fn: Callable[[str], None],
    detect_fn: Callable[[Path], str | None] | None = None,
) -> None:
    """`imp new` backstop: nudge when test_cmd_fast is absent and a command is detected."""
    from imp.cli.verify_detect import (
        detect_test_command,
        format_test_cmd_fast_yaml,
        resolve_test_cmd_choice,
    )
    from imp.providers.effective import load_effective_config
    detect = detect_fn or detect_test_command
    try:
        cfg = load_effective_config(
            project_root=project_root, control_root=control_root).config
    except (LoadError, LoadErrors):
        return
    if cfg.test_cmd_fast is not None:
        return
    suggested = detect(project_root)
    if suggested is None:
        return
    project_file = project_root / "imp" / "project.yaml"
    if non_interactive:
        err_fn(
            f"imp: no test_cmd_fast configured; detected `{suggested}`. Set "
            f"test_cmd_fast in {project_file} to gate regressions in-loop.")
        return
    chosen = resolve_test_cmd_choice(suggested, input_fn, output_fn)
    existing = project_file.read_text()
    with project_file.open("a") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        f.write(format_test_cmd_fast_yaml(chosen))


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
    except (GitSetupDeclined, IntegrationBranchUnresolved) as e:
        print(f"imp: {e}", file=sys.stderr)
        return 2
    except FileExistsError as e:
        print(f"imp: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nimp: init cancelled", file=sys.stderr)
        return 130
    if sys.stdout.isatty():
        print()
    return 0


def _run_header_line(quest_id: str, title: str, marker: str,
                     *, color: bool) -> str:
    stripped = title.strip()
    tail = f" · {stripped} · {marker}" if stripped else f" · {marker}"
    return (ansi(quest_id, *pal.NODE_COLORS["quest_id"], enabled=color)
            + ansi(tail, "dim", enabled=color))


def _print_run_header(quest_id: str, title: str, marker: str,
                      *, color: bool) -> None:
    print(_run_header_line(quest_id, title, marker, color=color))
    print()


def _print_opening(quest: Quest, marker: str, *, interactive: bool, color: bool) -> bool:
    """Lead with the run header, then the splash scene+phrase when interactive, so
    the quest identity is the first thing on screen. The header already emits its
    trailing blank line, so the splash is printed without its own leading one.
    Returns whether the splash was rendered."""
    _print_run_header(quest.id, quest.title, marker, color=color)
    return _print_splash_banner(interactive=interactive, lead_blank=False)


async def _read_intent_line(
    prompt: Any, quest: Any, *, color: bool, width: int,
    model_sink: dict[str, str] | None = None, hint: str = DEFAULT_HINT,
    toolbar: str | None = None,
) -> str:
    """Read one pre-session intent line, capturing a `/model` selection into `model_sink`."""
    while True:
        line = await ask_line_with_slash(
            prompt, None, quest, color=color, width=width, hint=hint,
            pre_session=True, toolbar_override=toolbar)
        if isinstance(line, SkillRequest):
            # Opening prompt is pre-session: no daemon gate exists yet, so the
            # skill body is loaded here and becomes the opening message.
            body = load_user_skill(quest.workflow_dir / "skills", line.name)
            return (
                f"{body.rstrip()}\n\n{line.args}\n" if line.args else body)
        if line.strip().startswith("/model "):
            if model_sink is not None:
                model_sink["model"] = line.strip()[len("/model "):].strip()
            continue
        if line == "/abandon":
            # No daemon pre-session; _abandon's rc threads out through SystemExit.
            raise SystemExit(_abandon(quest.control_root, quest.id))
        return line


async def _read_opening_message(
    quest: Quest, *, leave_mode: str, banner_shown: bool, color: bool,
    width: int, editing_mode: str,
    model_sink: dict[str, str] | None = None,
    _prompt_factory: Callable[[], Any] | None = None,
) -> str | None:
    """Lead with the run header, then the splash (when interactive), then read the
    opening message — so the quest identity is the first thing on screen.
    `banner_shown` is True on an interactive entry that should render the splash."""
    _print_opening(quest, leave_mode, interactive=banner_shown, color=color)
    make = _prompt_factory or (
        lambda: Prompt(skills_dir=quest.workflow_dir / "skills",
                       editing_mode=editing_mode, color=color, quest=quest))
    prompt = make()
    reply = await _read_intent_line(
        prompt, quest, color=color, width=width, model_sink=model_sink)
    return _normalize_opening_reply(reply)


async def _run_cleared_session(
    control: Path, base: str, *, leave_state: dict[str, str],
    leave_mode: str = "new",
) -> int:
    """Open a fresh imp new-style draft in place after /clear."""
    from imp.cli.signals import InterruptCoordinator

    color = should_use_color(sys.stdout)
    quest = Quest.create_draft(
        control, title="", quest_id=new_quest_id(existing_stems=live_stems(control)),
        base_branch=base, origin="draft")
    _session_started = False
    try:
        print(_run_header_line(quest.id, quest.title, leave_mode, color=color))
        _print_splash_banner(interactive=True)
        term_title = TerminalTitle(
            sys.stdout, quest.id, quest.title, initial="idle")
        prompt = Prompt(skills_dir=quest.workflow_dir / "skills",
                        editing_mode=_editing_mode(control), color=color, quest=quest)
        # A `/model` typed at this reopened prompt lands here; empty when none.
        cleared_model_sink: dict[str, str] = {}
        try:
            reply = await _read_intent_line(
                prompt, quest, color=color, width=_term_width(),
                model_sink=cleared_model_sink)
        except (EOFError, KeyboardInterrupt):
            if term_title is not None:
                term_title.reset()
            print(pal.caret(enabled=color))
            print()
            return 130
        _session_started = True
        cleared_model = cleared_model_sink.get("model")
        opening_text = _normalize_opening_reply(reply)
        # Persist the intent durably so the reopened chat's opening step seeds from
        # quest.json (mirroring the imp new path), surviving any later resume.
        quest.set_seed_opening(opening_text)
        status = LiveStatus(sys.stdout)
        driver = LiveDriver(
            stream=sys.stdout, color=color, width=_term_width,
            display_width=_display_width, status=status,
            quest_id=quest.id, title=quest.title)
        coord = InterruptCoordinator()
        solicit = make_interactive_solicit(
            quest, status=status, driver=driver, coord=coord)
        return await run_session_client(
            quest, driver=driver, coord=coord, solicit=solicit,
            status=status,
            leave_mode=leave_mode,
            leave_state=leave_state, title=term_title, model=cleared_model)
    finally:
        if not _session_started:
            discard_conversation(quest)


async def _clear_loop(
    control: Path,
    *,
    first: Callable[[dict[str, str]], Awaitable[int]],
    leave_mode: str = "new",
) -> int:
    """Run first, then loop only while /clear: open a fresh new-style chat."""
    leave_state: dict[str, str] = {}
    rc = await first(leave_state)
    while leave_state.get("intent") == "clear":
        leave_state = {}
        base = worktree.current_branch(control)
        rc = await _run_cleared_session(
            control, base, leave_state=leave_state, leave_mode=leave_mode)
    return rc


def _record_idea(args: argparse.Namespace, control: Path, base: str) -> int:
    title = args.idea
    if not title.strip():
        print("imp: --backlog title must be non-empty", file=sys.stderr)
        return 2
    description = getattr(args, "opening", None) or ""
    seed = backlog.seed_opening({"title": title, "description": description})
    quest_id = new_quest_id(existing_stems=live_stems(control))
    try:
        Quest.create_draft(
            control, title=title, quest_id=quest_id, base_branch=base,
            depends_on=list(getattr(args, "depends_on", None) or []),
            workflow=getattr(args, "workflow", None),
            origin="backlog", seed_opening=seed)
    except (LoadError, LoadErrors) as e:
        print(f"imp: {e}", file=sys.stderr)
        return 2
    _emit_human([f"imp: recorded backlog quest {quest_id} "
                 f"(start it with `imp resume {quest_id}`)"])
    return 0


def _select_headless(
    args: argparse.Namespace, *, stdin: Any = None, stdout: Any = None,
) -> tuple[bool, bool]:
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    non_interactive = bool(getattr(args, "non_interactive", False))
    interactive = not non_interactive and stdin.isatty() and stdout.isatty()
    headless = non_interactive or not interactive
    return headless, headless and not non_interactive


async def _run_fresh_session(
    quest: Quest, *, args: argparse.Namespace,
) -> int:
    """Run a freshly-created quest (new / materialized backlog) through
    the per-quest session daemon, dispatching on headless / interactive."""
    from imp.cli.signals import InterruptCoordinator

    _print_run_header(
        quest.id, quest.title, "new", color=should_use_color(sys.stdout))
    headless, auto_headless = _select_headless(args)
    autonomous = bool(getattr(args, "autonomous", False))
    if headless:
        if auto_headless:
            print(f"imp: no tty detected — running headless; resume with "
                  f"`imp resume {quest.id}`", file=sys.stderr)
        status = LiveStatus(sys.stdout)
        driver = LiveDriver(stream=sys.stdout,
                            color=should_use_color(sys.stdout),
                            width=_term_width, display_width=_display_width, status=status,
                            quest_id=quest.id, title=quest.title)
        coord = InterruptCoordinator()
        solicit = make_headless_solicit(getattr(args, "message", None), coord)
        return await run_session_client(
            quest, driver=driver, coord=coord, solicit=solicit,
            status=status, non_interactive=True,
            autonomous=autonomous)

    async def _first(leave_state: dict[str, str]) -> int:
        status = LiveStatus(sys.stdout)
        driver = LiveDriver(stream=sys.stdout,
                            color=should_use_color(sys.stdout),
                            width=_term_width, display_width=_display_width, status=status,
                            quest_id=quest.id, title=quest.title)
        if quest.seed_opening:
            driver.seed_opening_intent(quest.seed_opening)
        coord = InterruptCoordinator()
        solicit = make_interactive_solicit(
            quest, status=status, driver=driver, coord=coord)
        return await run_session_client(
            quest, driver=driver, coord=coord, solicit=solicit,
            status=status, leave_state=leave_state,
            autonomous=autonomous)

    return await _clear_loop(quest.control_root, first=_first)


def _backlog_run_only_flag(args: argparse.Namespace) -> str | None:
    if getattr(args, "non_interactive", False):
        return "--non-interactive"
    if getattr(args, "message", None) is not None:
        return "--message"
    if getattr(args, "autonomous", False):
        return "--autonomous"
    return None


class BaseResolutionError(Exception):
    """imp new cannot resolve a valid base branch — `default-base` is missing,
    or the resolved / `--base` override does not name an existing branch."""


def _resolve_new_base(control: Path, base_flag: str | None) -> str:
    """The validated branch `imp new` bases a quest on.

    `--base BRANCH` overrides the configured `default-base`; either way the
    branch must already exist. There is no runtime auto-detection and no
    current-branch fallback — a missing `default-base` or a non-existent branch
    is a hard error (BaseResolutionError), raised before any draft is created.
    `.` and `HEAD` are ordinary names here: they fail the existence check like
    any other unknown branch.
    """
    if base_flag is not None:
        if not worktree.branch_exists(control, base_flag):
            raise BaseResolutionError(
                f"--base {base_flag!r} does not name an existing branch.")
        return base_flag
    base = parse_default_base(control / "imp" / "project.yaml")
    if base is None:
        raise BaseResolutionError(
            "no `default-base` in imp/project.yaml — run `imp init` to set the "
            "branch quests integrate into.")
    if not worktree.branch_exists(control, base):
        raise BaseResolutionError(
            f"default-base {base!r} (imp/project.yaml) does not name an existing "
            "branch — update it or re-run `imp init`.")
    return base


async def _async_cmd_new(args: argparse.Namespace, *, leave_mode: str = "new") -> int:
    install_process_group()
    from imp.cli.signals import InterruptCoordinator

    control = _control_root()
    worktree.check_preconditions(control)
    worktree.ensure_imp_ignored(control)
    try:
        base = _resolve_new_base(control, getattr(args, "base", None))
    except BaseResolutionError as e:
        print(f"imp: {e}", file=sys.stderr)
        return 2

    wf_arg = getattr(args, "workflow", None)
    if wf_arg is not None and not validate_workflow_name(wf_arg):
        print(f"imp: invalid workflow name {wf_arg!r}", file=sys.stderr)
        return 2

    if getattr(args, "idea", None) is not None:
        if getattr(args, "title", None):
            print("imp: --backlog and --title cannot be combined "
                  "(--backlog is the title)", file=sys.stderr)
            return 2
        run_only = _backlog_run_only_flag(args)
        if run_only is not None:
            print(f"imp: --backlog files an idea instantly and never runs; "
                  f"{run_only} is for headless\nruns. Drop one. Start a filed "
                  f"idea later with `imp resume <id>`.", file=sys.stderr)
            return 2
        if not args.idea.strip():
            print("imp: --backlog title must be non-empty", file=sys.stderr)
            return 2
        return _record_idea(args, control, base)

    try:
        seed = resolve_spec_seed(args)
    except (SeedError, ValueError) as e:
        print(f"imp: {e}", file=sys.stderr)
        return 2

    block = _preflight_imp_init(control_root=control, project_root=control)
    if block is not None:
        print(f"imp: {block}", file=sys.stderr)
        return 2

    model_arg = getattr(args, "model", None)
    if model_arg is not None:
        from imp.cli.model_command import validate_model_available
        from imp.providers.effective import load_effective_config
        from imp.providers.resolve import available_providers
        config = load_effective_config(
            project_root=control, control_root=control).config
        reason = validate_model_available(
            model_arg, available=available_providers(config))
        if reason is not None:
            print(f"imp: {reason}", file=sys.stderr)
            return 2

    non_interactive = bool(getattr(args, "non_interactive", False))
    autonomous = bool(getattr(args, "autonomous", False))
    _test_cmd_fast_nudge(
        project_root=control, control_root=control,
        non_interactive=non_interactive,
        input_fn=input, output_fn=print,
        err_fn=lambda m: print(m, file=sys.stderr))

    interactive = _is_interactive(non_interactive=non_interactive)
    opening_text = seed.seed_text
    color = should_use_color(sys.stdout)

    quest_id = new_quest_id(existing_stems=live_stems(control))
    # No auto-name: an unnamed quest has an empty title. The agent
    # names it via rename_quest once intent is clear, or the user passes --title.
    title = seed.title if seed.title is not None else ""
    try:
        quest = Quest.create_draft(
            control, title=title, quest_id=quest_id, base_branch=base,
            workflow=wf_arg, origin="draft",
            depends_on=None, seed_opening=None)
    except (LoadError, LoadErrors) as e:
        print(f"imp: {e}", file=sys.stderr)
        return 2

    # Build the title before the opening prompt so the escape is written at startup,
    # not after the user answers. The opening prompt means the machine is idle
    # (blocked on the user) with no daemon event yet to correct the glyph.
    show_opening_prompt = _should_prompt_opening(
        seed_text=seed.seed_text, interactive=interactive)
    term_title = TerminalTitle(
        sys.stdout, quest.id, quest.title,
        initial="idle" if show_opening_prompt else "busy")

    # Print the run header before the prompt so erase/reprint causes no layout jump.
    header_printed = False
    # A `/model` typed at the opening prompt lands here; empty when none was typed.
    opening_model_sink: dict[str, str] = {}
    if show_opening_prompt:
        try:
            async with prompt_sigint_guard():
                reply = await _read_opening_message(
                    quest, leave_mode=leave_mode, banner_shown=interactive,
                    color=color, width=_term_width(),
                    editing_mode=_editing_mode(control),
                    model_sink=opening_model_sink)
        except (EOFError, KeyboardInterrupt):
            if term_title is not None:
                term_title.reset()
            discard_conversation(quest)   # drop the just-minted draft on cancel
            print(pal.caret(enabled=color))
            print()
            return 130
        header_printed = True
        # An empty reply falls back to the unseeded path (the opening step
        # opens by asking); a real answer becomes the opening intent.
        opening_text = reply
    opening_model = opening_model_sink.get("model") or model_arg

    # Persist the finalized intent so it survives any resume (including a restart
    # from an errored transcript). Covers both the positional and interactive
    # forms; an empty/absent reply persists as None/empty (unseeded).
    quest.set_seed_opening(opening_text)

    headless, auto_headless = _select_headless(args)
    if headless:
        if auto_headless:
            print(f"imp: no tty detected — running headless; resume with "
                  f"`imp resume {quest.id}`", file=sys.stderr)
        if not header_printed:
            _print_run_header(
                quest.id, quest.title, leave_mode, color=should_use_color(sys.stdout))
        status = LiveStatus(sys.stdout)
        driver = LiveDriver(stream=sys.stdout,
                            color=should_use_color(sys.stdout),
                            width=_term_width, display_width=_display_width, status=status,
                            quest_id=quest.id, title=quest.title)
        coord = InterruptCoordinator()
        solicit = make_headless_solicit(getattr(args, "message", None), coord)
        return await run_session_client(
            quest, driver=driver, coord=coord, solicit=solicit,
            status=status,
            title=term_title,
            non_interactive=True, autonomous=autonomous, model=opening_model)

    if not header_printed:
        _print_opening(quest, leave_mode, interactive=interactive, color=color)

    async def _first(leave_state: dict[str, str]) -> int:
        status = LiveStatus(sys.stdout)
        driver = LiveDriver(stream=sys.stdout,
                            color=should_use_color(sys.stdout),
                            width=_term_width, display_width=_display_width, status=status,
                            quest_id=quest.id, title=quest.title)
        if quest.seed_opening:
            driver.seed_opening_intent(quest.seed_opening)
        coord = InterruptCoordinator()
        solicit = make_interactive_solicit(
            quest, status=status, driver=driver, coord=coord)
        return await run_session_client(
            quest, driver=driver, coord=coord, solicit=solicit,
            status=status,
            leave_mode=leave_mode,
            leave_state=leave_state, title=term_title, autonomous=autonomous,
            model=opening_model)

    return await _clear_loop(control, first=_first, leave_mode=leave_mode)


def _cmd_new(args: argparse.Namespace) -> int:
    return anyio.run(functools.partial(_async_cmd_new, leave_mode="new"), args)


def _cmd_chat(args: argparse.Namespace) -> int:
    return anyio.run(functools.partial(_async_cmd_new, leave_mode="chat"), args)


def _resume_opens_fresh(quest: Quest) -> bool:
    """Whether a resume should open a fresh seeded session rather than replay history.

    True only for an un-materialized seed draft that has never run a step; a
    quest that has ever run (and is therefore archived-resumable) always
    replays. Reads the durable per-quest fact (State.has_started) from the
    persisted state.json the resume flow already rehydrated -- never a re-scan
    of events.jsonl."""
    if quest.materialized or not quest.seed_opening:
        return False
    return not State.load(quest.state_dir).has_started


def _warn_if_blocked(quest: Quest) -> None:
    try:
        deps = json.loads((quest.quest_dir / "quest.json").read_bytes()).get("depends_on", [])
    except (FileNotFoundError, json.JSONDecodeError):
        return
    r = make_store(quest.control_root).backlog_readiness(deps)
    ready, unmerged = r.ready, list(r.blocked_on)
    if not ready:
        print(f"imp: backlog quest {quest.id} is blocked on unmerged dep(s): "
              f"{', '.join(unmerged)}; starting anyway.", file=sys.stderr)


def _build_resume_driver(
    *, quest: Quest, state: State, status: LiveStatus,
    color: bool, preceded_by_blank: bool = False,
) -> LiveDriver:
    # startup_verb is drawn once at construction — compose_startup stores it, never re-draws.
    return LiveDriver(
        stream=sys.stdout, color=color,
        width=_term_width, display_width=_display_width, status=status,
        suppress_replay=True, preceded_by_blank=preceded_by_blank,
        startup_verb=state.step_label or random_status_phrase(),
        quest_id=quest.id, title=quest.title)


async def _async_cmd_resume(args: argparse.Namespace) -> int:
    install_process_group()
    from imp.cli.signals import InterruptCoordinator

    control = _control_root()
    target = await _resolve_resume_target(
        control, args.token,
        non_interactive=bool(getattr(args, "non_interactive", False)))
    if isinstance(target, int):
        return target
    quest_id = target
    loaded = _load_or_rehydrate(control, quest_id)
    if isinstance(loaded, int):
        return loaded
    quest = loaded
    if _resume_opens_fresh(quest):
        _warn_if_blocked(quest)
        return await _run_fresh_session(quest, args=args)
    state = State.load(quest.state_dir)
    # Persisted status is never `running` (it is derived from the lock at display
    # time); a crashed run is left at its resumable at-rest status (new/paused).
    if state.status not in RESUMABLE:
        print(
            f"imp: quest {quest.id} status is {state.status!r}; "
            "nothing to resume",
            file=sys.stderr,
        )
        return 2

    headless, auto_headless = _select_headless(args)
    autonomous = bool(getattr(args, "autonomous", False))
    if headless:
        if auto_headless:
            print(f"imp: no tty detected — running headless; resume with "
                  f"`imp resume {quest.id}`", file=sys.stderr)
        print(_run_header_line(
            quest.id, quest.title, "resuming", color=should_use_color(sys.stdout)))
        status = LiveStatus(sys.stdout)
        driver = _build_resume_driver(
            quest=quest, state=state, status=status,
            color=should_use_color(sys.stdout))
        coord = InterruptCoordinator()
        solicit = make_headless_solicit(getattr(args, "message", None), coord)
        return await run_session_client(
            quest, driver=driver, coord=coord, solicit=solicit,
            resume=True, status=status, non_interactive=True, autonomous=autonomous)
    if not getattr(args, "message", None):
        question = bare_turn_end_question(quest)
        if question is not None:
            return await _resume_inert(quest, question, autonomous=autonomous)

    # Interactive resume: a thin session-client; the daemon owns the worktree +
    # lock and runs resume prep + the agent loop.
    color = should_use_color(sys.stdout)
    print(_run_header_line(quest.id, quest.title, "resuming", color=color))
    splash_shown = _print_splash_banner(interactive=_is_interactive(
        non_interactive=bool(getattr(args, "non_interactive", False))))

    async def _first(leave_state: dict[str, str]) -> int:
        status = LiveStatus(sys.stdout)
        driver = _build_resume_driver(
            quest=quest, state=state, status=status,
            color=should_use_color(sys.stdout), preceded_by_blank=splash_shown)
        coord = InterruptCoordinator()
        solicit = make_interactive_solicit(
            quest, status=status, driver=driver, coord=coord)
        return await run_session_client(
            quest, driver=driver, coord=coord, solicit=solicit,
            resume=True, status=status, leave_state=leave_state,
            autonomous=autonomous)

    return await _clear_loop(quest.control_root, first=_first)


def _cmd_resume(args: argparse.Namespace) -> int:
    return anyio.run(_async_cmd_resume, args)


def _prelaunch_position_model(quest: Quest, state: State) -> str | None:
    """Resolve the resumed step's model from state.json; None when unresolved."""
    if state.step is None or state.resolved_tiers is None:
        return None
    from imp.engine.loader import load_workflow
    from imp.engine.model_override import current_provider_model
    workflow = load_workflow(
        quest.workflow_dir, project_keys=frozenset(state.project_facts or {}))
    try:
        return current_provider_model(state, workflow, state.step).model
    except (StopIteration, KeyError):
        return None


async def _inert_ask_line(quest: Quest, *, toolbar: str | None = None) -> str | None:
    """Read one line at inert resume through the shared slash-aware reader
    (session-verb rejection, /help, /skills, empty-submit guard); EOF / Ctrl-C
    retains the `> ` caret and maps to None — the quest stays parked."""
    color = should_use_color(sys.stdout)
    width = _term_width()
    prompt = Prompt(
        skills_dir=quest.workflow_dir / "skills",
        editing_mode=_editing_mode(quest.control_root),
        color=color, quest=quest)
    prompt.seed_history(
        _prompt_history_lines(quest.seed_opening, quest.event_log().read()))
    try:
        return await _read_intent_line(
            prompt, quest, color=color, width=width, toolbar=toolbar)
    except (EOFError, KeyboardInterrupt):
        print(pal.caret(enabled=color))
        print()
        return None


async def _resume_inert(
    quest: Quest, question: str, *, autonomous: bool = False,
) -> int:
    """AC8 inert resume: show the pending question, wait for a reply, launch live session."""
    from imp.cli.signals import InterruptCoordinator

    color = should_use_color(sys.stdout)
    term_title = TerminalTitle(sys.stdout, quest.id, quest.title, initial="idle")
    state = State.load(quest.state_dir)
    phase_head, _sep, plan_tail = (state.phase_label or "").partition(" · ")
    toolbar_row = compose_prelaunch_position_row(
        phase=phase_head or None, plan=plan_tail or None,
        step_label=state.step_label, model=_prelaunch_position_model(quest, state),
        quest_id=quest.id, title=quest.title,
        color=color, width=_term_width(), unsandboxed=state.unsandboxed)
    print(_run_header_line(quest.id, quest.title, "resuming", color=color))
    question = render_inert_question(question, width=_term_width(), color=color)

    async def _launch(line: str) -> int:
        async def _first(leave_state: dict[str, str]) -> int:
            status = LiveStatus(sys.stdout)
            driver = _build_resume_driver(
                quest=quest, state=state, status=status,
                color=color, preceded_by_blank=True)
            driver.echo_submitted_reply(line)   # visible echo, before the boot spinner
            coord = InterruptCoordinator()
            inner = make_interactive_solicit(
                quest, status=status, driver=driver, coord=coord)
            solicit = make_buffered_solicit(line, inner)
            return await run_session_client(
                quest, driver=driver, coord=coord, solicit=solicit,
                resume=True, status=status, leave_state=leave_state,
                title=term_title, autonomous=autonomous)

        return await _clear_loop(quest.control_root, first=_first)

    return await run_inert_resume(
        question=question,
        ask_line=lambda: _inert_ask_line(quest, toolbar=toolbar_row),
        launch=_launch)


def _editing_mode(control_root: Path) -> str:
    """Read editing_mode from the merged config; default to emacs if config is
    absent or invalid (run_quest reports a bad config with full context — don't
    double-fail here just to pick a prompt mode)."""
    from imp.providers.effective import load_effective_config
    try:
        return load_effective_config(
            project_root=control_root, control_root=control_root,
        ).config.editing_mode
    except (LoadError, LoadErrors):
        return "emacs"


_NO_WRAP_WIDTH = 10_000  # piped stdout, no explicit $COLUMNS => effectively no wrap


def _term_width() -> int:
    # WRAP width (not rule width _display_width): $COLUMNS overrides the piped no-wrap default.
    cols = os.environ.get("COLUMNS")
    if cols is not None:
        try:
            explicit = int(cols)
        except ValueError:
            explicit = 0
        if explicit > 0:
            return explicit
    if not sys.stdout.isatty():
        return _NO_WRAP_WIDTH
    return shutil.get_terminal_size((80, 24)).columns


def _display_width() -> int:
    # Physical terminal width via kernel winsize; ignores $COLUMNS (inverse of _term_width).
    try:
        return os.get_terminal_size(sys.stdout.fileno()).columns
    except OSError:
        return 80


def _list_quest_homes(control: Path) -> list[str]:
    """Ids of quests with a permanent root home under .imp/quests/."""
    return live_quest_ids(control)


def _quest_home(control: Path, quest_id: str) -> Path:
    return control / ".imp" / "quests" / quest_id


def _iter_quest_ids(control: Path) -> list[str]:
    """Live homes first, then archived ids not in the live set."""
    live = _list_quest_homes(control)
    seen = set(live)
    out = list(live)
    for cid in sorted(r.quest_id for r in make_store(control).completed_rows()):
        if cid not in seen:
            out.append(cid)
            seen.add(cid)
    return out


def _title_for_id(control: Path, quest_id: str) -> str | None:
    """Read a quest's current title (worktree home first, archive second)."""
    cdir = _quest_home(control, quest_id)
    cj = cdir / "quest.json"
    if cj.exists():
        try:
            return str(json.loads(cj.read_bytes())["title"])
        except (json.JSONDecodeError, KeyError):
            return None
    raw = archive_read.read_quest_json(control, quest_id)
    if raw is None:
        return None
    try:
        return str(json.loads(raw)["title"])
    except (json.JSONDecodeError, KeyError):
        return None


def _resolve_id(control: Path, token: str) -> str:
    """Resolve a user-typed token to a quest id (spec AC 7): it must be an
    unambiguous substring of exactly one quest's id. Multiple matches exit
    non-zero listing candidates; none errors."""
    if not token.strip():
        print(f"imp: no quest matches {token!r}", file=sys.stderr)
        raise SystemExit(2)
    matches = [cid for cid in _iter_quest_ids(control) if token in cid]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        _exit_ambiguous(token, matches, control)
    print(f"imp: no quest matches {token!r}", file=sys.stderr)
    raise SystemExit(2)


def _exit_ambiguous(token: str, candidates: list[str], control: Path) -> NoReturn:
    print(f"imp: {token!r} is ambiguous; candidates:", file=sys.stderr)
    color = should_use_color(sys.stderr)
    now = datetime.now(UTC)
    print(quest_row.header_line(color=color), file=sys.stderr)
    for cid in sorted(candidates):
        row = _row_for_id(control, cid)
        if row is None:
            print(f"  {cid}", file=sys.stderr)
            continue
        print(quest_row.format_quest_line(
            quest_id=row[0], title=row[4], status=row[1], step=row[2],
            step_label=row[6], last_iso=row[3], now=now, color=color,
            name_width=quest_row.name_budget()), file=sys.stderr)
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


def _show_artifact(control: Path, quest_id: str, rel: str) -> bytes | None:
    live = _quest_home(control, quest_id) / "artifacts" / rel
    if live.is_file():
        return live.read_bytes()
    return archive_read.read_artifact(control, quest_id, rel)


def _user_message_text(payload: dict[str, Any]) -> str:
    return "".join(
        b.get("text", "") for b in (payload.get("content") or [])
        if isinstance(b, dict) and b.get("type") == "text")


def _prompt_history_lines(
    seed_opening: str | None, events: Iterable[dict[str, Any]],
) -> list[str]:
    lines: list[str] = []
    if seed_opening and seed_opening.strip():
        lines.append(seed_opening)
    for ev in events:
        if ev.get("type") != "user_message":
            continue
        payload = ev.get("payload") or {}
        if payload.get("human") is not True:
            continue
        text = _user_message_text(payload)
        if text.strip():
            lines.append(text)
    return lines


def _first_user_message(events: list[dict[str, Any]]) -> str | None:
    for ev in events:
        if ev.get("type") == "user_message":
            text = _user_message_text(ev.get("payload") or {}).strip()
            if text:
                return text
    return None


def _first_genuine_human_turn(events: list[dict[str, Any]]) -> str | None:
    """First user_message a human actually typed: one that answers an
    awaiting_user_input solicitation, or one tagged by an immediately following
    user_interjected event. The composed task prompt -- a synthetic user_message
    that is neither solicited nor interjected -- is skipped. Mirrors the
    genuine-turn test narrative.py applies statefully while rendering."""
    awaiting = False
    for i, ev in enumerate(events):
        etype = ev.get("type")
        if etype == "awaiting_user_input":
            awaiting = True
            continue
        if etype != "user_message":
            continue
        nxt = events[i + 1] if i + 1 < len(events) else None
        interjected = nxt is not None and nxt.get("type") == "user_interjected"
        if awaiting or interjected:
            text = _user_message_text(ev.get("payload") or {}).strip()
            if text:
                return text
        awaiting = False
    return None


def _resolve_intent(events: list[dict[str, Any]]) -> str | None:
    for ev in events:
        if ev.get("type") == "opening_intent":
            text = ((ev.get("payload") or {}).get("text") or "").strip()
            if text:
                return text
    return _first_genuine_human_turn(events)


def _quest_meta(control: Path, quest_id: str) -> dict[str, Any] | None:
    cdir = _quest_home(control, quest_id)
    raw: bytes | None
    if (cdir / "quest.json").exists():
        raw = (cdir / "quest.json").read_bytes()
    else:
        raw = archive_read.read_quest_json(control, quest_id)
    if raw is None:
        return None
    try:
        result: dict[str, Any] = json.loads(raw)
        return result
    except json.JSONDecodeError:
        return None


def _quest_state(control: Path, quest_id: str) -> dict[str, Any] | None:
    cdir = _quest_home(control, quest_id)
    sj = cdir / "state" / "state.json"
    raw = sj.read_bytes() if sj.exists() else archive_read.read_state_json(control, quest_id)
    if raw is None:
        return None
    try:
        result: dict[str, Any] = json.loads(raw)
        return result
    except json.JSONDecodeError:
        return None


def _is_backlog_home(
    meta: dict[str, Any], *, conversed: bool, lock_live: Callable[[], bool]
) -> bool:
    """Shared zone predicate. A live quest home is Backlog iff it is
    un-materialized, has a non-draft origin, has never been conversed (no
    user_message event), and is not lock-live. The detail card
    (`_unstarted_backlog_meta`) and both overview zone builders (`_status_rows`,
    `_backlog_rows`) classify through this one helper, so a resumed/lock-live
    seed leaves Backlog and the three classifiers cannot drift."""
    return (
        not meta.get("materialized", True)
        and meta.get("origin", "draft") != "draft"
        and not conversed
        and not lock_live()
    )


def _unstarted_backlog_meta(control: Path, quest_id: str) -> dict[str, Any] | None:
    """quest.json of a LIVE un-materialized, non-draft-origin, never-conversed,
    unlocked Backlog home (origin `backlog`) — sourced from the worktree home
    only, never the archive: an abandoned home leaves an archived
    materialized:false quest.json that would otherwise render as a queued
    `status: backlog` brief instead of a History item. None for any archived-only
    id or any home the shared backlog predicate rejects (materialized,
    draft-origin, already-conversed, or lock-live)."""
    cdir = _quest_home(control, quest_id)
    cj = cdir / "quest.json"
    if not cj.exists():
        return None
    try:
        meta: dict[str, Any] = json.loads(cj.read_bytes())
    except json.JSONDecodeError:
        return None
    if meta.get("materialized", True):
        return None  # materialized homes are never Backlog; skip the event-log read
    try:
        Quest.load(control, quest_id)
    except (LoadError, LoadErrors):
        return None
    conversed = _first_user_message(
        make_store(control).get_events(quest_id) or []) is not None
    if not _is_backlog_home(meta, conversed=conversed,
                            lock_live=lambda: lock_is_live(cdir / "lock")):
        return None
    return meta


def _card_field(label: str, value: str, width: int, *, color: bool) -> str:
    """A card metadata row: the `label:` glyph dimmed (SGR 2 when color is on),
    padded to `width` columns, then the plain value at its aligned column."""
    return pal.ansi(label, "dim", enabled=color) + " " * (width - len(label)) + value


def _backlog_card(meta: dict[str, Any], control: Path, *, now: datetime,
                  color: bool) -> list[str]:
    r = make_store(control).backlog_readiness(meta.get("depends_on", []))
    ready, unmerged = r.ready, list(r.blocked_on)
    readiness = "ready" if ready else f"blocked on {', '.join(unmerged)}"
    created = meta.get("created_at")
    lines = [
        _card_field("id:", meta["id"], 12, color=color),
        _card_field("title:", meta.get("title") or "(unnamed)", 12, color=color),
        _card_field("status:", "backlog", 12, color=color),
        _card_field("provenance:", meta.get("origin", "draft"), 12, color=color),
        _card_field("readiness:", readiness, 12, color=color),
        _card_field(
            "created:",
            f"{quest_row.format_timestamp(created)} "
            f"({quest_row.relative_age(created, now=now)})",
            12, color=color),
    ]
    if meta.get("depends_on"):
        lines.append(_card_field("depends_on:", ", ".join(meta["depends_on"]),
                                 12, color=color))
    body = meta.get("seed_opening") or ""
    title = meta.get("title") or ""
    if title and (body == title or body.startswith(title + "\n")):
        body = body[len(title):].lstrip("\n")
    if body:
        lines.append("")
        lines.append(pal.ansi("intent:", "dim", enabled=color))
        lines.append(body)
    return lines


def _quest_card(control: Path, quest_id: str, *, now: datetime,
                color: bool) -> list[str]:
    card = make_store(control).quest_card(quest_id)
    if card is None:
        return []
    lines = [
        _card_field("id:", card.quest_id, 15, color=color),
        _card_field("title:", card.title.strip() or "(unnamed)", 15, color=color),
        _card_field("status:", card.status, 15, color=color),
    ]
    if card.status in _INFLIGHT and card.step not in ("", "-"):
        step_text = card.step_label or card.step
        position = f"{card.phase_label} · {step_text}" if card.phase_label else step_text
        lines.append(pal.clip_display(
            _card_field("step:", position, 15, color=color), _display_width()))
    lines.append(_card_field(
        "created:",
        f"{quest_row.format_timestamp(card.created)} "
        f"({quest_row.relative_age(card.created, now=now)})",
        15, color=color))
    lines.append(_card_field(
        "last activity:", quest_row.relative_age(card.last, now=now), 15, color=color))
    if card.base_branch:
        lines.append(_card_field("base_branch:", card.base_branch, 15, color=color))
    if card.merge_commit:
        lines.append(_card_field("merge_commit:", card.merge_commit, 15, color=color))
    if card.depends_on:
        lines.append(_card_field("depends_on:", ", ".join(card.depends_on),
                                 15, color=color))
    if card.intent:
        lines.append("")
        lines.append(pal.ansi("intent:", "dim", enabled=color))
        lines.append(card.intent)
    if card.summary:
        lines.append("")
        lines.append(pal.ansi("summary:", "dim", enabled=color))
        lines.extend(
            card.summary.decode("utf-8", errors="replace").rstrip("\n").splitlines())
    return lines


def _detail_card(control: Path, quest_id: str, *, now: datetime,
                 color: bool) -> list[str]:
    meta = _unstarted_backlog_meta(control, quest_id)
    if meta is not None:
        return _backlog_card(meta, control, now=now, color=color)
    return _quest_card(control, quest_id, now=now, color=color)


def _quest_json(control: Path, quest_id: str) -> dict[str, Any] | None:
    card = make_store(control).quest_card(quest_id)
    if card is None:
        return None
    data: dict[str, Any] = {
        "id": card.quest_id,
        "title": card.title.strip() or None,
        "status": card.status,
        "created": card.created or None,
        "last_activity": card.last or None,
        "base_branch": card.base_branch,
        "depends_on": card.depends_on,
        "intent": card.intent,
        "summary": None,
    }
    if card.status in _INFLIGHT and card.step not in ("", "-"):
        data["step"] = {"phase": card.phase_label or None,
                        "name": card.step_label or card.step}
    if card.summary:
        data["summary"] = card.summary.decode("utf-8", errors="replace").strip() or None
    return data


def _backlog_json(meta: dict[str, Any], control: Path) -> dict[str, Any]:
    r = make_store(control).backlog_readiness(meta.get("depends_on", []))
    ready, unmerged = r.ready, list(r.blocked_on)
    body = meta.get("seed_opening") or ""
    title = meta.get("title") or ""
    if title and (body == title or body.startswith(title + "\n")):
        body = body[len(title):].lstrip("\n")
    return {
        "id": meta["id"],
        "title": meta.get("title") or None,
        "status": "backlog",
        "provenance": meta.get("origin", "draft"),
        "readiness": "ready" if ready else "blocked",
        "blocked_on": list(unmerged),
        "created": meta.get("created_at"),
        "depends_on": meta.get("depends_on", []),
        "body": body or None,
    }


def _cmd_list(args: argparse.Namespace) -> int:
    try:
        control = _control_root()
    except worktree.PreconditionError:
        _emit_human([_NO_QUESTS_MESSAGE])
        return 0
    now = datetime.now(UTC)
    color = should_use_color(sys.stdout)
    dispositions = _selected_list_dispositions(args)
    if not dispositions and args.since is None:
        lines = _overview_lines(control, show_all=args.all, now=now, color=color)
        _page_or_print(lines, no_pager=args.no_pager, trailing_blank=True)
        return 0
    try:
        cutoff = _since_cutoff(args.since, now) if args.since is not None else None
    except ValueError:
        print(_SINCE_ERROR, file=sys.stderr)
        raise SystemExit(2) from None
    selected = dispositions or {"active", "backlog", "merged"}
    lines = _filtered_overview_lines(
        control, dispositions=selected, cutoff=cutoff, now=now, color=color)
    _page_or_print(lines, no_pager=args.no_pager, trailing_blank=True)
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    token = args.token
    if token is None and not _interactive():
        print("imp: 'imp show' needs a quest id — run 'imp list' for an overview",
              file=sys.stderr)
        return 2
    control = _control_root()
    if token is None:
        picked = anyio.run(_pick_show_target, control)
        if isinstance(picked, int):
            return picked
        quest_id = picked
    else:
        # Same comprehension as _resolve_id — picker and resolver agree on candidates.
        matches = [cid for cid in _iter_quest_ids(control) if token in cid]
        if _interactive() and token.strip() and len(matches) >= 2:
            picked = anyio.run(_pick_show_matches, control, matches)
            if isinstance(picked, int):
                return picked
            quest_id = picked
        else:
            try:
                quest_id = _resolve_id(control, token)
            except SystemExit:
                return 2
    resource = args.resource
    meta = _unstarted_backlog_meta(control, quest_id)
    if meta is not None:
        if resource is None:
            if args.json:
                _page_or_print(
                    json.dumps(_backlog_json(meta, control), indent=2).splitlines(),
                    no_pager=args.no_pager)
                return 0
            _page_or_print(
                _backlog_card(meta, control, now=datetime.now(UTC),
                              color=should_use_color(sys.stdout)),
                no_pager=args.no_pager, trailing_blank=True)
            return 0
        print(f"imp: {quest_id} is in the backlog (not started); "
              f"no {resource} yet", file=sys.stderr)
        return 2
    if resource is None:
        if args.json:
            card = _quest_json(control, quest_id)
            if card is None:
                print(f"imp: no such quest {quest_id!r}", file=sys.stderr)
                return 2
            _page_or_print(json.dumps(card, indent=2).splitlines(),
                           no_pager=args.no_pager)
            return 0
        _page_or_print(
            _detail_card(control, quest_id, now=datetime.now(UTC),
                         color=should_use_color(sys.stdout)),
            no_pager=args.no_pager, trailing_blank=True)
        return 0
    if resource == "spec":
        data = make_store(control).get_artifact(quest_id, "spec")
    if data is None:
        print(f"imp: no {resource} for {quest_id!r}", file=sys.stderr)
        return 2
    _page_or_print(data.decode("utf-8", errors="replace").splitlines(),
                   no_pager=args.no_pager, trailing_blank=True)
    return 0


def _load_events_for_id(control: Path, quest_id: str) -> list[dict[str, Any]] | None:
    """Worktree first, archive second. Returns None if the id is in neither.

    Worktree-first is necessary, not merely preferred: an id lives in exactly
    one place (the archive snapshot is written once, concurrent with worktree
    removal), so the only overlap is a crash mid-sequence where worktree-first
    harmlessly returns identical content."""
    try:
        quest = Quest.load(control, quest_id)
    except LoadError:
        quest = None
    if quest is not None:
        return list(quest.event_log().read())
    raw = archive_read.read_events(control, quest_id)
    if raw is None:
        return None
    return _parse_jsonl(raw)


def _resolve_row_intent(control: Path, r: _Row) -> str | None:
    """Fallback display name for a title-less row: its opening prompt, else None."""
    if r[4].strip():
        return None
    return _resolve_intent(_load_events_for_id(control, r[0]) or [])


def _resolve_default_id(control: Path) -> str | None:
    """No-arg default: most-recent quest home by its last event ts, else
    the most-recent archived id (by the index's last_event_ts), else None."""
    store = make_store(control)
    homes = store.scan_homes()
    if homes:
        return max(homes, key=lambda h: (h.last_iso, h.quest_id)).quest_id
    dated = [r for r in store.completed_rows() if r.last]
    if not dated:
        return None
    return max(dated, key=lambda r: (r.last, r.quest_id)).quest_id


_NO_QUESTS_MESSAGE = "No quests yet — start one with imp new"


def _log_picker_empty() -> int:
    _emit_human([_NO_QUESTS_MESSAGE])
    return 2


_LOG_PICKER_HEADER = (
    "Show log for which quest?  (↑/↓ to move · enter to select · q to cancel)"
)


async def _pick_full_pool_target(control: Path, *, header: str) -> str | int:
    """No-token picker over the full pool, shared by the log and events pickers."""
    rows = _all_quest_rows(control)
    default_id = _resolve_default_id(control)

    def _non_tty(_rows: list[_Row]) -> str | int:
        return default_id if default_id is not None else 2

    selected_index = next(
        (i for i, r in enumerate(rows) if r[0] == default_id), 0)

    return await _pick_quest_target(
        control=control,
        rows=rows,
        header=header,
        on_empty=_log_picker_empty,
        on_non_tty=_non_tty,
        selected_index=selected_index,
    )


async def _pick_log_target(control: Path) -> str | int:
    return await _pick_full_pool_target(control, header=_LOG_PICKER_HEADER)


def _with_trailing_blank(lines: list[str]) -> list[str]:
    """Strip trailing empty strings, then append exactly one blank line."""
    out = list(lines)
    while out and out[-1] == "":
        out.pop()
    out.append("")
    return out


def _emit_human(lines: Iterable[str]) -> None:
    """Print lines to stdout; add one trailing blank line only on a tty."""
    out = list(lines)
    if sys.stdout.isatty():
        out = _with_trailing_blank(out)
    for line in out:
        print(line)


def _page_or_print(lines: Iterable[str], *, no_pager: bool,
                   trailing_blank: bool = False) -> None:
    # Pages lines through $PAGER on a tty; falls back to print() otherwise.
    if trailing_blank and sys.stdout.isatty():
        lines = _with_trailing_blank(list(lines))
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


_LOG_EXPORT_DEFAULT = object()
"""Sentinel `const` for `--html`/`--md` given without a FILE argument."""


def _follow_conflict(args: argparse.Namespace) -> str | None:
    """Return the first output-mode flag `--follow` conflicts with, or None."""
    for name, present in (
        ("--summary", args.summary),
        ("--full", args.full),
        ("--show/--hide", bool(args.show or args.hide)),
        ("--html", args.html is not None),
        ("--md", args.md is not None),
    ):
        if present:
            return name
    return None


def _follow_log(events: list[dict[str, Any]]) -> int:
    """Replay a quest's narrative through the live run view's renderer."""
    status = LiveStatus(sys.stdout)
    driver = LiveDriver(
        stream=sys.stdout, color=should_use_color(sys.stdout),
        width=_term_width, display_width=_display_width, status=status)
    for event in events:
        driver.on_event(event)
    driver.finish()
    return 0


class _EventTail:
    """Offset-tracked, partial-line-tolerant reader for a live events.jsonl.

    Reads only complete, newline-terminated lines from a kept-open descriptor.
    A complete line that fails to parse (a transient btrfs+zstd read fault, or a
    torn append) halts consumption at that byte offset and is retried on the
    next read — it never raises. `emitted` counts every event handed out."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fp: Any = None
        self._offset = 0
        self.emitted = 0

    def read_new(self) -> list[dict[str, Any]]:
        if self._fp is None:
            try:
                self._fp = self._path.open("rb")
            except FileNotFoundError:
                return []
        self._fp.seek(self._offset)
        data = self._fp.read()
        events: list[dict[str, Any]] = []
        consumed = 0
        for chunk in data.split(b"\n")[:-1]:
            try:
                events.append(json.loads(chunk.decode("utf-8")))
            except (json.JSONDecodeError, UnicodeDecodeError):
                break
            consumed += len(chunk) + 1
        self._offset += consumed
        self.emitted += len(events)
        return events


_FOLLOW_TERMINAL = frozenset({"completed", "abandoned", "saved"})


def _follow_paused_status(quest_id: str) -> Callable[[int], list[str]]:
    """A static one-row footer composer shown while the run is paused."""
    line = f"paused — resume with imp resume {quest_id}"
    return lambda frame: [line]


def _drain_from_archive(driver: LiveDriver, tail: _EventTail,
                        control: Path, quest_id: str) -> int:
    """The home vanished mid-follow: feed the archived events beyond the count
    already emitted (none if the id is not archived), finish, and exit 0."""
    raw = archive_read.read_events(control, quest_id)
    if raw is not None:
        for event in _parse_jsonl(raw)[tail.emitted:]:
            driver.on_event(event)
    driver.finish()
    return 0


def _follow_step(driver: LiveDriver, tail: _EventTail, status: LiveStatus,
                 control: Path, quest_id: str, home: Path) -> int | None:
    """One poll iteration. Feeds newly-appended events through the driver and
    drives the footer from the lock's liveness. Returns an exit code when the
    follow should stop (terminal status, or the home vanished into the archive),
    else None to keep polling."""
    if not home.exists():
        return _drain_from_archive(driver, tail, control, quest_id)
    for event in tail.read_new():
        driver.on_event(event)
    try:
        state_status = State.load(home / "state").status
    except FileNotFoundError:
        return _drain_from_archive(driver, tail, control, quest_id)
    if state_status in _FOLLOW_TERMINAL:
        for event in tail.read_new():
            driver.on_event(event)
        driver.finish()
        return 0
    if lock_is_live(home / "lock"):
        status.update(driver.compose_status)
    else:
        status.update(_follow_paused_status(quest_id))
    return None


_FOLLOW_POLL_INTERVAL = 0.3


def _follow_live(control: Path, quest_id: str, home: Path) -> int:
    """Replay a live quest's narrative through LiveDriver, then tail
    events.jsonl until a terminal status or the home is archived away. Ctrl-C
    clears the footer and exits 130."""
    status = LiveStatus(sys.stdout)
    driver = LiveDriver(
        stream=sys.stdout, color=should_use_color(sys.stdout),
        width=_term_width, display_width=_display_width, status=status)
    tail = _EventTail(home / "events.jsonl")
    try:
        return anyio.run(
            _follow_poll_loop, driver, tail, status, control, quest_id, home)
    except KeyboardInterrupt:
        status.reset()
        return 130


async def _follow_poll_loop(
    driver: LiveDriver, tail: _EventTail, status: LiveStatus,
    control: Path, quest_id: str, home: Path,
) -> int:
    """Advance the spinner (LiveStatus.tick) concurrently with a poll loop that
    feeds new events and checks exit conditions every _FOLLOW_POLL_INTERVAL."""
    rc = 0
    async with anyio.create_task_group() as tg:
        tg.start_soon(status.tick)
        while True:
            step = _follow_step(driver, tail, status, control, quest_id, home)
            if step is not None:
                rc = step
                break
            await anyio.sleep(_FOLLOW_POLL_INTERVAL)
        tg.cancel_scope.cancel()
    return rc


def _log_show_set(full: bool, show: list[str], hide: list[str]) -> frozenset[str]:
    """Resolve imp log's content categories: --full=all, --show adds, --hide removes."""
    from imp.cli.render_text import LOG_CATEGORIES
    base = set(LOG_CATEGORIES) if full else set()
    return frozenset((base | set(show)) - set(hide))


def _select_log_events(
    args: argparse.Namespace, quest_id: str, events: list[dict[str, Any]],
) -> list[dict[str, Any]] | int:
    """Filter events by imp log's windowing options, or return exit code 2 on bad input."""
    from imp.engine import events_index as ei

    range_specs = list(args.range) if args.range else []
    if args.range_pos:
        range_specs.append(args.range_pos)
    steps = set(args.step) if args.step else None
    agents = set(args.agent) if args.agent else None
    if not (steps or agents or range_specs or args.limit is not None):
        return events

    indexed = list(enumerate(events, start=1))
    handle_map = ei.subagent_handle_map(indexed)
    max_ord = indexed[-1][0] if indexed else 0
    intervals: list[tuple[int, int]] = []
    try:
        for spec in range_specs:
            intervals.extend(ei.parse_range_spec(spec, max_ord))
    except ei.RangeSpecError as exc:
        print(f"imp: {exc}", file=sys.stderr)
        return 2
    if steps is not None:
        known = {s.name for s in ei.step_windows(indexed)}
        missing = sorted(steps - known)
        if missing:
            print(f"imp: no step {missing[0]!r} in {quest_id}", file=sys.stderr)
            return 2
    selected = ei.select_event_lines(
        indexed, agents=agents, ranges=intervals or None,
        step_names=steps, limit=args.limit, handle_map=handle_map)
    return [ev for _, ev in selected]


def _cmd_log(args: argparse.Namespace) -> int:
    from imp.cli import narrative
    from imp.cli.render_text import LOG_CATEGORIES, render_tree

    show_names = args.show or []
    hide_names = args.hide or []
    unknown = sorted((set(show_names) | set(hide_names)) - set(LOG_CATEGORIES))
    if unknown:
        print(f"imp log: error: unknown category {unknown[0]!r} "
              f"(choose from: {', '.join(LOG_CATEGORIES)})", file=sys.stderr)
        return 2
    show = _log_show_set(args.full, show_names, hide_names)

    control = _control_root()
    if args.follow:
        conflict = _follow_conflict(args)
        if conflict is not None:
            print(f"imp log: error: argument -f/--follow: not allowed with "
                  f"argument {conflict}", file=sys.stderr)
            return 2
        args.no_pager = True
    quest_id: str | None
    if args.token is not None:
        try:
            quest_id = _resolve_id(control, args.token)
        except SystemExit:
            return 2
    elif _interactive():
        target = anyio.run(_pick_log_target, control)
        if isinstance(target, int):
            return target
        quest_id = target
    else:
        quest_id = _resolve_default_id(control)
    if quest_id is None:
        _emit_human([_NO_QUESTS_MESSAGE])
        return 0
    meta = _unstarted_backlog_meta(control, quest_id)
    if meta is not None:
        lines: Iterable[str] = _backlog_card(
            meta, control, now=datetime.now(UTC),
            color=should_use_color(sys.stdout))
        _page_or_print(lines, no_pager=args.no_pager, trailing_blank=True)
        return 0
    store = make_store(control)
    events = store.get_events(quest_id)
    if events is None:
        print(f"imp: no such quest {quest_id!r}", file=sys.stderr)
        return 2

    if args.follow:
        if os.environ.get("IMP_SESSION_SOCKET"):
            print("imp log: live follow is unavailable inside the sandbox; "
                  "showing a one-shot snapshot.", file=sys.stderr)
            return _follow_log(events)
        home = _quest_home(control, quest_id)
        if home.exists():
            return _follow_live(control, quest_id, home)
        return _follow_log(events)

    selected = _select_log_events(args, quest_id, events)
    if isinstance(selected, int):
        return selected
    events = selected

    if args.html is not None or args.md is not None:
        return _write_log_export(args, control, store, quest_id, events)

    width = _term_width()
    color = should_use_color(sys.stdout)

    if args.summary:
        from imp.cli.render_summary import (
            SummaryHeader,
            build_summary,
            render_summary,
            summary_total,
        )
        card = store.quest_card(quest_id)
        status = card.status if card is not None else "?"
        workflow = parse_default_workflow(control / "imp" / "project.yaml")
        header = SummaryHeader(workflow=workflow, quest_id=quest_id,
                               status=status, total_seconds=summary_total(events))
        lines = render_summary(build_summary(events), header=header,
                               color=color, width=width)
        _page_or_print(lines, no_pager=args.no_pager, trailing_blank=True)
        return 0

    tree = narrative.build_tree(events)
    lines = render_tree(tree, show=show, color=color, width=width,
                        display_width=_display_width())
    _page_or_print(lines, no_pager=args.no_pager, trailing_blank=True)
    return 0


def _write_log_export(args: argparse.Namespace, control: Path, store: Store,
                      quest_id: str, events: list[dict[str, Any]]) -> int:
    if args.summary:
        from imp.cli.render_markdown import render_summary_markdown
        from imp.cli.render_summary import (
            SummaryHeader,
            build_summary,
            summary_total,
        )
        card = store.quest_card(quest_id)
        status = card.status if card is not None else "?"
        workflow = parse_default_workflow(control / "imp" / "project.yaml")
        header = SummaryHeader(workflow=workflow, quest_id=quest_id,
                               status=status, total_seconds=summary_total(events))
        document = render_summary_markdown(build_summary(events), header=header)
    else:
        from imp.cli import narrative
        from imp.cli.render_markdown import render_markdown
        document = render_markdown(narrative.build_tree(events), full=args.full)
    if args.html is not None:
        from imp.cli.render_html import render_html
        document = render_html(document)
        ext = "html"
        target = args.html
    else:
        ext = "md"
        target = args.md
    if target == "-":
        sys.stdout.write(document)
        return 0
    path = Path(f"imp-log-{quest_id}.{ext}") if target is _LOG_EXPORT_DEFAULT else Path(target)
    path.write_text(document, encoding="utf-8")
    print(path)
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


def _journal_last_iso(control: Path, quest_id: str) -> str | None:
    for r in make_store(control).completed_rows():
        if r.quest_id == quest_id:
            return r.last or None
    return None


def _journal_status(control: Path, quest_id: str) -> str | None:
    return archive_read.status_of(control, quest_id)


def _resume_gone(control: Path, quest_id: str) -> int:
    status = _journal_status(control, quest_id)
    title = _title_for_id(control, quest_id) or quest_id
    if status == "abandoned":
        print(f"{title} was abandoned; nothing to resume.")
        return 0
    if status is not None:
        print(f"{title} is already completed; nothing to resume.")
        return 0
    print(f"imp: no quest matches {quest_id!r}", file=sys.stderr)
    return 2


def _rehydrate_resumable(control: Path, quest_id: str) -> Quest | None:
    from imp.engine.archive import record, records_root
    rec = record.read_record(records_root(control), quest_id)
    if rec is None or not rec.get("resumable"):
        return None
    try:
        archive_read.rehydrate_quest(control, quest_id)
        return Quest.load(control, quest_id)
    except (FileExistsError, LoadError, LoadErrors):
        return None


def _load_or_rehydrate(control: Path, quest_id: str) -> Quest | int:
    try:
        return Quest.load(control, quest_id)
    except LayoutMismatch as e:
        print(f"imp: {e}", file=sys.stderr)
        return 2
    except (LoadError, LoadErrors):
        quest = _rehydrate_resumable(control, quest_id)
        if quest is None:
            return _resume_gone(control, quest_id)
        return quest


# Spec §10: in-flight statuses; completed quests live in the archive.
_INFLIGHT = ("running", "new", "paused", "merge_blocked", "draft")
RESUMABLE = ("new", "paused", "merge_blocked", "draft", "saved")


def resolve_display_status(
    status: str, quest_dir: Path, *, materialized: bool
) -> str:
    """The one map from persisted (at-rest) status to display status.

    `running` is derived, never persisted: a live process holds quest_dir/lock
    with its pid inside for its whole run. A stale lock (file present, pid dead)
    reads as not-running, so the quest shows its resumable at-rest status — no
    manufactured `errored`. An un-materialized new/paused quest shows as
    `draft`, generalizing the already-accepted draft synthesis."""
    if lock_is_live(quest_dir / "lock"):
        return "running"
    if status in ("new", "paused") and not materialized:
        return "draft"
    return status


# Row = (id, status, step, last_activity, title, created_at, step_label, phase_label).
# created_at is the trailing sort key only; step_label + phase_label feed the
# merged STATUS cell's ` (phase · step)` suffix (see quest_row). The step slot
# stays the RAW step name so imp list keeps printing it.
_Row = tuple[str, str, str, str, str, str, str | None, str | None]



def _events_have_user_message(events_path: Path) -> bool:
    """True iff events.jsonl holds at least one user_message event. Read line by
    line and short-circuit so a long log isn't fully parsed just to classify."""
    try:
        with events_path.open("rb") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("type") == "user_message":
                    return True
    except FileNotFoundError:
        return False
    return False



def _status_row(quest_id: str, state_json: bytes, quest_json: bytes,
                last_activity: str) -> _Row | None:
    """Pure (id, status, step, last_activity, title, created_at) extractor, fed
    by either worktree file bytes or archive bytes. The archived state.json and
    quest.json are schema-identical to the in-flight ones (both written by
    State.save / Quest.create), so the two sections can't silently drift.
    Returns None on malformed/incomplete JSON so a single corrupt entry can't
    crash the whole `imp list` listing (callers skip None)."""
    try:
        st = json.loads(state_json)
        meta = json.loads(quest_json)
        return (quest_id, st["status"], st.get("step") or "-", last_activity,
                meta["title"], meta["created_at"], st.get("step_label"),
                st.get("phase_label"))
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def _row_for_id(control: Path, quest_id: str) -> _Row | None:
    cdir = _quest_home(control, quest_id)
    # archive_read returns bytes | None; the live quest-home branch always finds
    # the files. Annotate so the None-guard below stays reachable (not dead code).
    state_json: bytes | None
    quest_json: bytes | None
    if (cdir / "quest.json").exists():
        # A quest home mid-creation has quest.json written but state.json not
        # yet (Quest.create is not atomic across the two files). The daemon's
        # scan loop can hit that window, so tolerate it like scan_homes
        # rather than crashing the whole listing.
        try:
            state_json = (cdir / "state" / "state.json").read_bytes()
            quest_json = (cdir / "quest.json").read_bytes()
        except FileNotFoundError:
            return None
        last = _last_event_iso(cdir / "events.jsonl") or ""
    else:
        state_json = archive_read.read_state_json(control, quest_id)
        quest_json = archive_read.read_quest_json(control, quest_id)
        if state_json is None or quest_json is None:
            return None
        last = _journal_last_iso(control, quest_id) or ""
    row = _status_row(quest_id, state_json, quest_json, last)
    if row is None:
        return None
    materialized = json.loads(quest_json).get("materialized", True)
    status = resolve_display_status(row[1], cdir, materialized=materialized)
    return (row[0], status, *row[2:])


def _status_rows(control: Path, scanned: list[HomeDigest] | None = None) -> list[_Row]:
    homes = make_store(control).scan_homes() if scanned is None else scanned
    rows: list[_Row] = []
    for h in homes:
        if h.state_json is None:
            continue
        cdir = _quest_home(control, h.quest_id)
        if _is_backlog_home(h.meta, conversed=h.has_user_message,
                            lock_live=lambda: lock_is_live(cdir / "lock")):  # noqa: B023
            continue  # un-materialized, never-conversed, unlocked seed -> Backlog
        row = _status_row(h.quest_id, h.state_json, h.quest_json, h.last_iso)
        if row is None:
            continue
        status = resolve_display_status(
            row[1], cdir, materialized=h.meta.get("materialized", True))
        row = (row[0], status, *row[2:])
        if row[1] not in _INFLIGHT:
            continue
        rows.append(row)
    return rows


def _completed_rows(control: Path, live_ids: set[str] | None = None) -> list[_Row]:
    """Completed/abandoned quests NOT currently surfaced in-flight, from a
    SINGLE indexed archive query (spec AC 7). `live_ids` lets the overview reuse
    the in-flight id set it already computed instead of re-deriving it."""
    live = {r[0] for r in _status_rows(control)} if live_ids is None else live_ids
    rows: list[_Row] = []
    for r in make_store(control).completed_rows():
        if r.quest_id in live or r.status in _INFLIGHT:
            continue
        rows.append((r.quest_id, r.status, r.step, r.last, r.title, r.created, None, None))
    return rows


def _shelved_rows(control: Path, live_ids: set[str] | None = None) -> list[_Row]:
    """Shelved conversations — archived final status `paused` with no live home —
    for the `--all` history block. Mirrors _completed_rows' live-id dedup."""
    live = {r[0] for r in _status_rows(control)} if live_ids is None else live_ids
    return [(r.quest_id, r.status, r.step, r.last, r.title, r.created, None, None)
            for r in make_store(control).completed_rows()
            if r.status == "paused" and r.quest_id not in live]


def _sort_rows_newest_first(rows: list[_Row]) -> list[_Row]:
    return sorted(rows, key=lambda r: (r[3], r[5]), reverse=True)


def _resumable_rows(control: Path) -> list[_Row]:
    rows = [r for r in _status_rows(control) if r[1] in RESUMABLE]
    rows += _backlog_rows(control)
    return _sort_rows_newest_first(rows)


def _abandon_rows(control: Path) -> list[_Row]:
    return _sort_rows_newest_first(_status_rows(control) + _backlog_rows(control))


def _resumable_match_rows(control: Path, token: str) -> list[_Row]:
    """Rows for resumable quests whose id contains `token`.

    Spans live-resumable rows (`_resumable_rows`) AND archived-but-resumable
    records — an archived id whose archive record reads `resumable=True` (a
    "shelved" conversation whose live home was deleted). A live row wins when an id
    appears in both. Archived completed/abandoned (non-resumable) ids are never
    matches, so a terminal id sharing a stem never joins the menu."""
    from imp.engine.archive import record, records_root

    rows = [r for r in _resumable_rows(control) if token in r[0]]
    live = {r[0] for r in rows}
    for cid in archive_read.archived_ids(control):
        if token not in cid or cid in live:
            continue
        rec = record.read_record(records_root(control), cid)
        if rec is None or not rec.get("resumable"):
            continue
        row = _row_for_id(control, cid)
        if row is not None:
            rows.append(row)
    return _sort_rows_newest_first(rows)


def _abandon_match_rows(control: Path, token: str) -> list[_Row]:
    """Abandon rows (live homes + backlog) whose id contains `token`."""
    return [r for r in _abandon_rows(control) if token in r[0]]


def _past_quest_count(control: Path) -> int:
    return len(_completed_rows(control))


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _resume_picker_empty(control: Path) -> int:
    print("Nothing to resume.")
    past = _past_quest_count(control)
    if past:
        plural = "s" if past != 1 else ""
        print(pal.ansi(
            f"({past} past quest{plural} — see `imp list --all`)",
            "dim", enabled=should_use_color(sys.stdout)))
    return 0


def _resume_picker_non_tty(rows: list[_Row]) -> str | int:
    if len(rows) == 1:
        return rows[0][0]
    print("Multiple resumable quests — pass a token:", file=sys.stderr)
    color = should_use_color(sys.stderr)
    now = datetime.now(UTC)
    print(quest_row.header_line(color=color), file=sys.stderr)
    name_w = quest_row.name_budget()
    for r in rows:
        print(quest_row.format_quest_line(
            quest_id=r[0], title=r[4], status=r[1], step=r[2], step_label=r[6],
            phase_label=r[7], last_iso=r[3], now=now, color=color,
            name_width=name_w), file=sys.stderr)
    return 2


async def _pick_quest_target(
    *, control: Path, rows: list[_Row], header: str,
    on_empty: Callable[[], int],
    on_non_tty: Callable[[list[_Row]], str | int],
    selected_index: int = 0,
    non_interactive: bool = False,
) -> str | int:
    if not rows:
        return on_empty()
    if non_interactive or not _interactive():
        return on_non_tty(rows)
    if len(rows) == 1:
        return rows[0][0]
    candidates = [resume_select.Candidate(
        quest_id=r[0], title=r[4], status=r[1], step=r[2], last_iso=r[3],
        step_label=r[6], phase_label=r[7],
        intent=_resolve_row_intent(control, r))
        for r in rows]
    chosen = await resume_select.run_picker(
        candidates, now=datetime.now(UTC),
        color=should_use_color(sys.stdout),
        header=header, selected_index=selected_index)
    return chosen if chosen is not None else 2


_RESUME_PICKER_HEADER = (
    "Resume which quest?  (↑/↓ to move · enter to resume · q to cancel)"
)


async def _pick_resume_target(control: Path, *, non_interactive: bool) -> str | int:
    return await _pick_quest_target(
        control=control,
        rows=_resumable_rows(control),
        header=_RESUME_PICKER_HEADER,
        on_empty=lambda: _resume_picker_empty(control),
        on_non_tty=_resume_picker_non_tty,
        non_interactive=non_interactive,
    )


async def _resolve_resume_target(
    control: Path, token: str | None, *, non_interactive: bool,
) -> str | int:
    if token is None or not token.strip():
        return await _pick_resume_target(control, non_interactive=non_interactive)
    matches = _resumable_match_rows(control, token)
    if len(matches) >= 2:
        return await _pick_quest_target(
            control=control,
            rows=matches,
            header=_RESUME_PICKER_HEADER,
            on_empty=lambda: _resume_picker_empty(control),
            on_non_tty=_resume_picker_non_tty,
            non_interactive=non_interactive,
        )
    if len(matches) == 1:
        return matches[0][0]
    try:
        return _resolve_id(control, token)
    except SystemExit:
        return 2


def _deliver_abandon_resident(control: Path, quest_id: str) -> None:
    from imp.remote.session.client import SessionClient
    from imp.remote.session.discover import session_socket_path
    from imp.remote.session.protocol import CONTROL_ABANDON

    async def _run() -> None:
        path = str(session_socket_path(control, quest_id))
        client = SessionClient(path, quest_id, "", on_event=lambda _: None)
        await client.connect()
        try:
            await client.send_control(CONTROL_ABANDON)
            with anyio.fail_after(10):
                await client.receive_events()
        finally:
            await client.aclose()

    anyio.run(_run)


def _abandon(control: Path, quest_id: str) -> int:
    """Tear down a quest (worktree, branch, state) as one logged operation.

    Order is pinned by git (spec §2): archive the runtime snapshot BEFORE
    worktree removal (records live inside the worktree); `git branch -D` AFTER
    (git refuses to delete a branch checked out in a worktree). Idempotent /
    crash-tolerant: re-running after any mid-sequence crash converges."""
    try:
        quest = Quest.load(control, quest_id)
    except (LoadError, LoadErrors):
        # No live or un-materialized quest-home: Quest.load failed because the
        # home directory is absent (already fully torn down, or never existed).
        # Idempotent teardown — reap a possibly-orphaned branch and exit 0.
        worktree.delete_branch(control, quest_id)
        _emit_human([f"quest {quest_id} already gone; ensured branch imp/{quest_id} is removed."])
        return 0

    token = quest.id
    if session_is_resident(control, quest_id):
        try:
            _deliver_abandon_resident(control, quest_id)
        except OSError:
            pass  # daemon gone, wedged, or startup-window hang (TimeoutError ⊂ OSError)
        else:
            _emit_human([f"Abandoned {token}."])
            return 0
    try:
        tear_down_quest(quest)
    except LockHeld as e:
        print(f"imp: quest {quest_id} is running (pid {e.held_by_pid or '?'}); "
              f"stop it before abandoning", file=sys.stderr)
        return 2
    _emit_human([f"Abandoned {token}."])
    return 0


def _cmd_remote_control_start(args: argparse.Namespace) -> int:
    from imp.remote.daemon.runner import start_daemon
    return start_daemon(_control_root())


def _cmd_remote_control_stop(args: argparse.Namespace) -> int:
    from imp.remote.daemon.runner import stop_daemon
    return stop_daemon(_control_root())


def _abandon_picker_empty() -> int:
    _emit_human(["Nothing to abandon."])
    return 2


def _abandon_picker_non_tty(rows: list[_Row]) -> int:
    print("imp: abandon needs a quest token on a non-interactive terminal",
          file=sys.stderr)
    return 2


_ABANDON_PICKER_HEADER = (
    "Abandon which quest?  (↑/↓ to move · enter to select · q to cancel)"
)


async def _pick_abandon_target(control: Path) -> str | int:
    return await _pick_quest_target(
        control=control,
        rows=_abandon_rows(control),
        header=_ABANDON_PICKER_HEADER,
        on_empty=_abandon_picker_empty,
        on_non_tty=_abandon_picker_non_tty,
    )


async def _pick_abandon_matches(
    control: Path, token: str, rows: list[_Row],
) -> str | int:
    """Scoped abandon picker over the token's live matches. On a non-tty it
    errors with a token-specific message (distinct from the no-token
    `_abandon_picker_non_tty` copy, which reads wrong when a token was given)."""
    def _non_tty(_rows: list[_Row]) -> int:
        print(f"imp: {token!r} matches multiple quests; pass a more specific token",
              file=sys.stderr)
        return 2

    return await _pick_quest_target(
        control=control,
        rows=rows,
        header=_ABANDON_PICKER_HEADER,
        on_empty=_abandon_picker_empty,
        on_non_tty=_non_tty,
    )


_SHOW_PICKER_HEADER = (
    "Show which quest?  (↑/↓ to move · enter to select · q to cancel)"
)


def _show_picker_empty() -> int:
    _emit_human(["No quests to show."])
    return 2


def _show_picker_non_tty(rows: list[_Row]) -> int:
    # Defensive parity only: _cmd_show handles the non-tty no-token case
    # directly (needs-a-quest-id error), so _pick_show_target is invoked
    # solely on an interactive tty and this branch is never reached.
    print("imp: 'imp show' needs a quest id — run 'imp list' for an overview",
          file=sys.stderr)
    return 2


def _all_quest_rows(control: Path) -> list[_Row]:
    """Display rows for the full-pool pickers: every _iter_quest_ids id
    (live homes + backlog + archived), via _row_for_id, unreadable ids
    dropped, newest-first — the same pool `imp show <exact-id>` reaches."""
    rows = [r for r in (_row_for_id(control, cid)
                        for cid in _iter_quest_ids(control)) if r is not None]
    return _sort_rows_newest_first(rows)


async def _pick_show_target(control: Path) -> str | int:
    return await _pick_quest_target(
        control=control,
        rows=_all_quest_rows(control),
        header=_SHOW_PICKER_HEADER,
        on_empty=_show_picker_empty,
        on_non_tty=_show_picker_non_tty,
    )


def _ambiguous_matches_non_tty(rows: list[_Row]) -> int:
    print("imp: ambiguous token; candidates:", file=sys.stderr)
    color = should_use_color(sys.stderr)
    now = datetime.now(UTC)
    print(quest_row.header_line(color=color), file=sys.stderr)
    name_w = quest_row.name_budget()
    for r in rows:
        print(quest_row.format_quest_line(
            quest_id=r[0], title=r[4], status=r[1], step=r[2],
            step_label=r[6], phase_label=r[7], last_iso=r[3], now=now, color=color,
            name_width=name_w), file=sys.stderr)
    return 2


async def _pick_id_matches(
    control: Path, matches: list[str], *,
    header: str, on_empty: Callable[[], int],
) -> str | int:
    """Scoped picker over a token's raw _iter_quest_ids matches (>=2)."""
    rows = _sort_rows_newest_first(
        [r for r in (_row_for_id(control, cid) for cid in matches)
         if r is not None])
    return await _pick_quest_target(
        control=control, rows=rows, header=header, on_empty=on_empty,
        on_non_tty=_ambiguous_matches_non_tty)


async def _pick_show_matches(control: Path, matches: list[str]) -> str | int:
    return await _pick_id_matches(
        control, matches, header=_SHOW_PICKER_HEADER, on_empty=_show_picker_empty)


_EVENTS_PICKER_HEADER = (
    "Show events for which quest?  (↑/↓ to move · enter to select · q to cancel)"
)


async def _pick_events_target(control: Path) -> str | int:
    """No-token events picker over the shared full pool. `_cmd_events` routes the
    non-tty no-token case to `_resolve_default_id` directly, so this runs only on a tty."""
    return await _pick_full_pool_target(control, header=_EVENTS_PICKER_HEADER)


async def _pick_events_matches(control: Path, matches: list[str]) -> str | int:
    return await _pick_id_matches(
        control, matches, header=_EVENTS_PICKER_HEADER, on_empty=_log_picker_empty)


def _cmd_abandon(args: argparse.Namespace) -> int:
    control = _control_root()
    token = args.token
    if token is None or not token.strip():
        target = anyio.run(_pick_abandon_target, control)
        if isinstance(target, int):
            return target
        return _abandon(control, target)
    matches = _abandon_match_rows(control, token)
    if len(matches) >= 2:
        target = anyio.run(_pick_abandon_matches, control, token, matches)
        if isinstance(target, int):
            return target
        return _abandon(control, target)
    if len(matches) == 1:
        return _abandon(control, matches[0][0])
    try:
        quest_id = _resolve_id(control, token)
    except SystemExit:
        # No live/backlog match, and _resolve_id found nothing live either. A
        # quest whose worktree was already removed (crash between abandon's
        # remove and branch-delete) is unresolvable there — but abandon archived
        # the id before removal, so recover it from the archive to still reap an
        # orphaned branch (AC 15c). Exact id, else unique suffix match.
        ids = archive_read.archived_ids(control)
        hits = [cid for cid in ids if cid == token] or \
               [cid for cid in ids if cid.endswith(token)]
        if len(hits) == 1:
            return _abandon(control, hits[0])
        # Truly unknown (never existed, or ambiguous) — nothing to reap.
        print(f"imp: no quest matches {token!r}", file=sys.stderr)
        return 2
    return _abandon(control, quest_id)


def _cmd_edit(args: argparse.Namespace) -> int:
    control = _control_root()
    quest_id = _resolve_id(control, args.token)
    quest = Quest.load(control, quest_id)

    zone_change = args.backlog or args.active
    dep_change = args.depends_on is not None or args.clear_deps
    if (args.title is None and not zone_change and not dep_change
            and args.append_intent is None):
        print("imp: imp edit needs at least one of --title, --backlog/--active, "
              "--depends-on/--clear-deps, or --append-intent", file=sys.stderr)
        return 2

    if zone_change and quest.materialized:
        print(f"imp: {quest_id} is already built; zone moves apply to "
              "un-materialized quests only (use abandon/pause instead)",
              file=sys.stderr)
        return 2

    if args.append_intent is not None:
        conversed = _first_user_message(list(quest.event_log().read())) is not None
        if quest.materialized or conversed:
            print(f"imp: {quest_id} has already started; --append-intent only "
                  "applies before a quest is picked up", file=sys.stderr)
            return 2

    if args.title is not None and args.title != quest.title:
        if quest.materialized:
            quest.rename(args.title, log=quest.event_log())
        else:
            quest.set_title(args.title)
    if args.backlog:
        quest.set_origin("backlog")
    elif args.active:
        quest.set_origin("draft")
    if args.clear_deps:
        quest.set_depends_on([])
    elif args.depends_on is not None:
        quest.set_depends_on(args.depends_on)
    if args.append_intent is not None:
        existing = quest.seed_opening or ""
        if existing:
            quest.set_seed_opening(existing + "\n\n" + args.append_intent)
        else:
            quest.set_seed_opening(args.append_intent)
    return 0


def _backlog_rows(control: Path, scanned: list[HomeDigest] | None = None) -> list[_Row]:
    homes = make_store(control).scan_homes() if scanned is None else scanned
    rows: list[_Row] = []
    for h in homes:
        meta = h.meta
        cdir = _quest_home(control, h.quest_id)
        if not _is_backlog_home(meta, conversed=h.has_user_message,
                                lock_live=lambda: lock_is_live(cdir / "lock")):  # noqa: B023
            continue  # materialized / draft-origin / conversed / lock-live -> Active
        origin = meta.get("origin", "draft")
        ready = make_store(control).backlog_readiness(meta.get("depends_on", [])).ready
        created = meta.get("created_at", "")
        blocked = "" if ready else "blocked"
        rows.append((h.quest_id, origin, blocked, h.last_iso,
                     meta.get("title") or "", created, blocked or None, None))
    return rows


_HISTORY_CAP = 10

_SINCE_ERROR = (
    "imp: --since expects 'today' or a number with a unit — "
    "h/d/w/m/y (e.g. 2d, 1w, 3m).")


def _since_cutoff(spec: str, now: datetime) -> datetime:
    """Resolve a --since SPEC to an absolute cutoff instant (ValueError if unrecognized)."""
    s = spec.strip().lower()
    local = now.astimezone()
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if s == "today":
        return midnight
    if s == "yesterday":
        return midnight - timedelta(days=1)
    if s == "this-week":
        return midnight - timedelta(days=local.weekday())
    if s == "this-month":
        return midnight.replace(day=1)
    match = re.fullmatch(r"(\d+)([hdwmy])", s)
    if match is None:
        raise ValueError(spec)
    n = int(match.group(1))
    unit = match.group(2)
    if unit == "h":
        return now - timedelta(hours=n)
    if unit == "d":
        return now - timedelta(days=n)
    if unit == "w":
        return now - timedelta(weeks=n)
    if unit == "m":
        return now - timedelta(days=30 * n)
    return now - timedelta(days=365 * n)  # unit == "y"


def _selected_list_dispositions(args: argparse.Namespace) -> set[str]:
    """Union of the disposition tokens selected by the `imp list` flags."""
    picked: set[str] = set()
    if args.active:
        picked.add("active")
    if args.backlog:
        picked.add("backlog")
    if args.merged:
        picked.add("merged")
    if args.abandoned:
        picked.add("abandoned")
    if args.shelved:
        picked.add("shelved")
    if args.archived:
        picked |= {"merged", "abandoned", "shelved"}
    return picked


# Display-only disposition word for the overview history block.
_DISPOSITION = {"completed": "merged", "abandoned": "abandoned", "paused": "shelved"}


def _history_rows(control: Path, *, show_all: bool,
                  live_ids: set[str] | None = None) -> list[_Row]:
    """History-block rows: completed/abandoned, plus shelved (paused) rows under --all."""
    rows = [r for r in _completed_rows(control, live_ids) if r[1] in _DISPOSITION]
    if show_all:
        rows += _shelved_rows(control, live_ids)
    else:
        rows = [r for r in rows if r[1] == "completed"]
    return _sort_rows_newest_first(rows)


def _overview_body(inflight: list[_Row], backlog_rows: list[_Row],
                   history: list[_Row], *, intents: dict[str, str | None],
                   history_label: str, now: datetime,
                   color: bool) -> list[str]:
    """Render the sectioned overview for already-gathered rows."""

    def _name_text(r: _Row) -> str:
        raw = r[4].strip()
        if raw:
            return raw
        intent = intents.get(r[0])
        return " ".join(intent.split()) if intent else "(unnamed)"

    specs: list[tuple[str, str | None, str]] = []
    for r in inflight:
        specs.append((r[1], quest_row.compose_status_label(r[2], r[6], r[7]),
                      _name_text(r)))
    for r in backlog_rows:
        specs.append((r[1], quest_row.compose_status_label(r[2], r[6], None),
                      _name_text(r)))
    for r in history:
        specs.append((_DISPOSITION[r[1]], None, _name_text(r)))

    status_width, name_width = quest_row.overview_widths(specs)

    lines = [quest_row.header_line(color=color, status_width=status_width)]
    for r in inflight:
        lines.append(quest_row.format_quest_line(
            quest_id=r[0], title=r[4], status=r[1], step=r[2], step_label=r[6],
            phase_label=r[7], last_iso=r[3], now=now, color=color,
            name_width=name_width, status_width=status_width,
            intent=intents.get(r[0])))
    if backlog_rows:
        lines.append("")
        lines.append(pal.ansi("backlog:", "dim", enabled=color))
        for r in backlog_rows:
            lines.append(quest_row.format_quest_line(
                quest_id=r[0], title=r[4], status=r[1], step=r[2],
                step_label=r[6], phase_label=None, last_iso=r[3], now=now,
                color=color, name_width=name_width, status_width=status_width,
                intent=intents.get(r[0])))
    if history:
        lines.append("")
        lines.append(pal.ansi(history_label, "dim", enabled=color))
        for r in history:
            lines.append(quest_row.format_quest_line(
                quest_id=r[0], title=r[4], status=_DISPOSITION[r[1]], step="-",
                last_iso=r[3], now=now, color=color, name_width=name_width,
                status_width=status_width, dim=True, intent=intents.get(r[0])))
    return lines


def _overview_lines(control: Path, *, show_all: bool, now: datetime,
                    color: bool) -> list[str]:
    """Overview lines across all zones, sharing one content-sized STATUS/NAME width."""
    scanned = make_store(control).scan_homes()
    status_rows = _status_rows(control, scanned)
    inflight = _sort_rows_newest_first(status_rows)
    backlog_rows = _sort_rows_newest_first(_backlog_rows(control, scanned))
    history = _history_rows(control, show_all=show_all,
                            live_ids={r[0] for r in status_rows})
    if not inflight and not backlog_rows and not history:
        return [_NO_QUESTS_MESSAGE]
    label = "history:" if show_all else "merged:"
    intents = {d.quest_id: d.intent_title for d in scanned}
    for r in history:
        if not r[4].strip() and r[0] not in intents:
            intents[r[0]] = _resolve_row_intent(control, r)
    return _overview_body(inflight, backlog_rows, history, intents=intents,
                          history_label=label, now=now, color=color)


_NO_FILTER_MATCH_MESSAGE = "No quests match that filter."

# Disposition token -> archived final-state status.
_ARCHIVE_STATUS = {"merged": "completed", "abandoned": "abandoned",
                   "shelved": "paused"}


def _row_since(r: _Row, cutoff: datetime | None) -> bool:
    """True when the row's last-activity timestamp is at or after the cutoff."""
    if cutoff is None:
        return True
    if not r[3]:
        return False
    try:
        return datetime.fromisoformat(r[3]) >= cutoff
    except (ValueError, TypeError):
        return False


def _filtered_overview_lines(control: Path, *, dispositions: set[str],
                              cutoff: datetime | None, now: datetime,
                              color: bool) -> list[str]:
    """Sectioned overview over the full uncapped set, filtered by disposition and cutoff."""
    scanned = make_store(control).scan_homes()
    status_rows = _status_rows(control, scanned)
    live_ids = {r[0] for r in status_rows}
    inflight = (_sort_rows_newest_first(status_rows)
                if "active" in dispositions else [])
    backlog_rows = (_sort_rows_newest_first(_backlog_rows(control, scanned))
                    if "backlog" in dispositions else [])
    wanted = {_ARCHIVE_STATUS[d]
              for d in dispositions & {"merged", "abandoned", "shelved"}}
    history_all = _completed_rows(control, live_ids) + _shelved_rows(control, live_ids)
    history = _sort_rows_newest_first([r for r in history_all if r[1] in wanted])

    inflight = [r for r in inflight if _row_since(r, cutoff)]
    backlog_rows = [r for r in backlog_rows if _row_since(r, cutoff)]
    history = [r for r in history if _row_since(r, cutoff)]

    if not inflight and not backlog_rows and not history:
        return [_NO_FILTER_MATCH_MESSAGE]
    intents = {d.quest_id: d.intent_title for d in scanned}
    for r in history:
        if not r[4].strip() and r[0] not in intents:
            intents[r[0]] = _resolve_row_intent(control, r)
    return _overview_body(inflight, backlog_rows, history, intents=intents,
                          history_label="history:", now=now, color=color)


def _cmd_status(args: argparse.Namespace, _control: Path | None = None) -> int:
    try:
        control = _control if _control is not None else _control_root()
    except worktree.PreconditionError:
        _emit_human([_NO_QUESTS_MESSAGE])
        return 0
    lines = _overview_lines(control, show_all=args.all, now=datetime.now(UTC),
                            color=should_use_color(sys.stdout))
    _emit_human(lines)
    return 0


_SEARCH_ZONES = ("active", "backlog", "archive")


def _selected_search(args: argparse.Namespace) -> tuple[bool, bool, set[str]]:
    """(search_active, search_backlog, archive_statuses) from the search flags."""
    statuses: set[str] = set()
    if args.merged:
        statuses.add("completed")
    if args.abandoned:
        statuses.add("abandoned")
    if args.shelved:
        statuses.add("paused")
    if args.archived:
        statuses |= {"completed", "abandoned", "paused"}
    if args.merged_after is not None:
        statuses.add("completed")
    if not (args.active or args.backlog or statuses):
        return True, True, {"completed", "abandoned", "paused"}
    return args.active, args.backlog, statuses


def _search_active(control: Path, query: str) -> list[SearchHit]:
    """In-flight + draft-origin quests whose id, title, or conversation prose
    contains `query` (case-insensitive)."""
    q = query.lower()
    scanned = make_store(control).scan_homes()
    prose_by_id = {d.quest_id: d.prose for d in scanned}
    hits: list[SearchHit] = []
    for r in _status_rows(control, scanned):
        prose = prose_by_id.get(r[0], "")
        if q in r[0].lower() or q in r[4].lower() or q in prose.lower():
            hits.append(SearchHit(r[0], r[4], "active", prose, r[3]))
    return hits


def _backlog_seed_opening(control: Path, quest_id: str) -> str:
    cj = _quest_home(control, quest_id) / "quest.json"
    try:
        meta = json.loads(cj.read_bytes())
    except (OSError, ValueError, TypeError):
        return ""
    return str(meta.get("seed_opening") or "")


def _search_backlog(control: Path, query: str) -> list[SearchHit]:
    """Backlog quests whose id, title, or seed_opening body (title + description)
    contains `query` (case-insensitive)."""
    q = query.lower()
    hits: list[SearchHit] = []
    for r in _backlog_rows(control):
        quest_id, title = r[0], r[4]
        seed = _backlog_seed_opening(control, quest_id)
        if q in quest_id.lower() or q in title.lower() or q in seed.lower():
            hits.append(SearchHit(quest_id, title, "backlog", seed, r[3]))
    return hits


def _search_archived(control: Path, query: str, statuses: set[str],
                     merged_after: str | None) -> list[SearchHit]:
    """Archive hits in `statuses`; completed hits also gated by merged_after."""
    hits: list[SearchHit] = []
    for cid, title, content, status, last in archive_read.search_archive_content(
            control, query):
        if status not in statuses:
            continue
        if (status == "completed" and merged_after is not None
                and not _merged_after(control, cid, merged_after)):
            continue
        hits.append(SearchHit(cid, title, "archive", content, last))
    return hits


def _selected_events_search(args: argparse.Namespace) -> tuple[bool, bool, bool]:
    """(active, backlog, archived) zone flags; none given searches all three."""
    if not (args.active or args.backlog or args.archived):
        return True, True, True
    return args.active, args.backlog, args.archived


def _matched_events(
    events: list[dict[str, Any]], query: str, *,
    types: set[str] | None, agents: set[str] | None,
) -> tuple[list[tuple[int, dict[str, Any]]], dict[str, str]]:
    """Matched (ordinal, event) pairs after type/agent filters, plus the handle map."""
    from imp.cli.render_helpers import event_text
    from imp.engine import events_index as ei
    indexed = list(enumerate(events, start=1))
    handle_map = ei.subagent_handle_map(indexed)
    candidates = ei.select_event_lines(
        indexed, types=types, agents=agents, handle_map=handle_map)
    q = query.lower()
    matched = [(o, ev) for o, ev in candidates if q in event_text(ev).lower()]
    return matched, handle_map


class EventSearchHit(NamedTuple):
    quest_id: str
    title: str
    zone: str
    last_activity: str | None
    events: list[tuple[int, dict[str, Any]]]
    handle_map: dict[str, str]
    match_count: int


def _events_search_active(
    control: Path, query: str, *, types: set[str] | None, agents: set[str] | None,
) -> list[EventSearchHit]:
    """In-flight quests with >=1 event matching query (after type/agent filters)."""
    store = make_store(control)
    scanned = store.scan_homes()
    hits: list[EventSearchHit] = []
    for r in _status_rows(control, scanned):
        events = store.get_events(r[0]) or []
        matched, handle_map = _matched_events(events, query, types=types, agents=agents)
        if matched:
            hits.append(EventSearchHit(
                r[0], r[4], "active", r[3], matched, handle_map, len(matched)))
    return hits


def _events_search_backlog(
    control: Path, query: str, *, types: set[str] | None, agents: set[str] | None,
) -> list[EventSearchHit]:
    """Backlog quests matched on id/title/intent. Skipped when type/agent filter is set."""
    if types is not None or agents is not None:
        return []
    q = query.lower()
    hits: list[EventSearchHit] = []
    for r in _backlog_rows(control):
        quest_id, title = r[0], r[4]
        seed = _backlog_seed_opening(control, quest_id)
        if q in quest_id.lower() or q in title.lower() or q in seed.lower():
            hits.append(EventSearchHit(quest_id, title, "backlog", r[3], [], {}, 1))
    return hits


def _events_search_archived(
    control: Path, query: str, *, types: set[str] | None, agents: set[str] | None,
) -> list[EventSearchHit]:
    """Archived quests: index candidates re-scanned to specific matching events."""
    store = make_store(control)
    hits: list[EventSearchHit] = []
    for cid, title, _status, last in archive_read.search_archive_events_content(control, query):
        events = store.get_events(cid) or []
        matched, handle_map = _matched_events(events, query, types=types, agents=agents)
        if matched:
            hits.append(EventSearchHit(
                cid, title, "archive", last, matched, handle_map, len(matched)))
    return hits


def _events_search(
    control: Path, query: str, zones: tuple[bool, bool, bool], *,
    types: set[str] | None, agents: set[str] | None,
) -> list[EventSearchHit]:
    active, backlog, archived = zones
    hits: list[EventSearchHit] = []
    if active:
        hits += _events_search_active(control, query, types=types, agents=agents)
    if backlog:
        hits += _events_search_backlog(control, query, types=types, agents=agents)
    if archived:
        hits += _events_search_archived(control, query, types=types, agents=agents)
    return hits


def _merged_after(control: Path, quest_id: str, base_commit: str) -> bool:
    """True iff the recorded merge landed AFTER base_commit; fail-safe False otherwise."""
    merge_commit = archive_read.merge_commit_of(control, quest_id)
    if not merge_commit:
        return False
    # git exit 1 = not an ancestor (landed after) → keep; exit 0/any-other = exclude (fail-safe).
    return worktree._git(
        control, "merge-base", "--is-ancestor", merge_commit, base_commit,
    ).returncode == 1


def _spine_segment(text: str, query: str, *base_codes: str, color: bool) -> str:
    """Paint text in base_codes, over-painting each query run bold-yellow."""
    if not color:
        return text
    if not query:
        return pal.ansi(text, *base_codes, enabled=True)
    out: list[str] = []
    last = 0
    for m in re.finditer(re.escape(query), text, flags=re.IGNORECASE):
        if m.start() > last:
            out.append(pal.ansi(text[last:m.start()], *base_codes, enabled=True))
        out.append(pal.ansi(m.group(), "bold", "yellow", enabled=True))
        last = m.end()
    if last < len(text):
        out.append(pal.ansi(text[last:], *base_codes, enabled=True))
    return "".join(out)


def _highlight(text: str, query: str, *, color: bool) -> str:
    """Wrap every case-insensitive occurrence of `query` in `text` with a bold-yellow accent."""
    if not color or not query:
        return text
    out: list[str] = []
    last = 0
    for m in re.finditer(re.escape(query), text, flags=re.IGNORECASE):
        out.append(text[last:m.start()])
        out.append(pal.ansi(m.group(), "bold", "yellow", enabled=True))
        last = m.end()
    out.append(text[last:])
    return "".join(out)


def _excerpts(
    content: str, query: str, *, color: bool, limit: int = 3,
) -> tuple[list[str], int]:
    """Up to `limit` grep-style match spans — whitespace-collapsed,
    query-highlighted, `…`-truncated toward the terminal width — plus the count
    of query occurrences beyond `limit`."""
    if not query:
        return [], 0
    width = shutil.get_terminal_size((80, 24)).columns
    context = max(20, (width - len(query) - 6) // 2)
    matches = list(re.finditer(re.escape(query), content, flags=re.IGNORECASE))
    spans: list[str] = []
    for m in matches[:limit]:
        start = max(0, m.start() - context)
        end = min(len(content), m.end() + context)
        body = " ".join(content[start:end].split())
        prefix = "… " if start > 0 else ""
        suffix = " …" if end < len(content) else ""
        spans.append(prefix + _highlight(body, query, color=color) + suffix)
    return spans, max(0, len(matches) - limit)


def _render_search_hit(
    hit: SearchHit, query: str, *, now: datetime, color: bool,
) -> list[str]:
    lines = [_search_list_line(hit, query=query, now=now, color=color)]
    spans, extra = _excerpts(hit.content, query, color=color)
    lines.extend(f"  {s}" for s in spans)
    if extra > 0:
        lines.append(pal.ansi(f"  … +{extra} more", "dim", enabled=color))
    return lines


def _search_list_line(
    hit: SearchHit, *, query: str, now: datetime, color: bool,
) -> str:
    """Spine heading id · title · zone · age; query highlighted; title dropped when empty."""
    id_seg = _spine_segment(hit.quest_id, query, *pal.NODE_COLORS["quest_id"],
                            color=color)
    age = quest_row.relative_age(hit.last_activity, now=now)
    tail = pal.ansi(f"{hit.zone} · {age}", "dim", enabled=color)
    title = hit.title.strip()
    if not title:
        return f"{id_seg} · {tail}"
    title_seg = _spine_segment(title, query, color=color)
    return f"{id_seg} · {title_seg} · {tail}"


def _cmd_search(args: argparse.Namespace, _control: Path | None = None) -> int:
    control = _control if _control is not None else _control_root()
    want_active, want_backlog, statuses = _selected_search(args)
    zones = SearchZones(active=want_active, backlog=want_backlog,
                        statuses=frozenset(statuses), merged_after=args.merged_after)
    hits: list[SearchHit] = list(make_store(control).search(args.query, zones))
    if not hits:
        _emit_human([f"No quests match {args.query!r}."])
        return 0
    now = datetime.now(UTC)
    color = should_use_color(sys.stdout)
    zone_order = {z: i for i, z in enumerate(_SEARCH_ZONES)}
    hits.sort(key=lambda h: h.last_activity or "", reverse=True)
    hits.sort(key=lambda h: zone_order[h.zone])
    if args.list:
        lines = [_search_list_line(h, query=args.query, now=now, color=color)
                 for h in hits]
    else:
        noun = "match" if len(hits) == 1 else "matches"
        lines = [pal.ansi(f'{len(hits)} {noun} for "{args.query}"',
                          "dim", enabled=color)]
        for h in hits:
            lines.append("")
            lines.extend(_render_search_hit(h, args.query, now=now, color=color))
    _page_or_print(lines, no_pager=args.no_pager, trailing_blank=True)
    return 0


def _events_search_list_line(
    hit: EventSearchHit, *, query: str, color: bool,
) -> str:
    id_seg = _spine_segment(hit.quest_id, query, *pal.NODE_COLORS["quest_id"],
                            color=color)
    tail = pal.ansi(hit.zone, "dim", enabled=color)
    title = hit.title.strip()
    if not title:
        return f"{id_seg} · {tail}"
    title_seg = _spine_segment(title, query, color=color)
    return f"{id_seg} · {title_seg} · {tail}"


def _render_events_search_hit(
    hit: EventSearchHit, query: str, *, now: datetime, color: bool,
    width: int, limit: int = 3,
) -> list[str]:
    from imp.engine import events_index as ei
    spine = _search_list_line(
        SearchHit(hit.quest_id, hit.title, hit.zone, "", hit.last_activity),
        query=query, now=now, color=color)
    lines = [spine]
    for ordinal, event in hit.events[:limit]:
        digest = ei.digest_line(ordinal, event, hit.handle_map, width=width)
        lines.append("  " + _highlight(digest, query, color=color))
    extra = len(hit.events) - limit
    if extra > 0:
        lines.append(pal.ansi(f"  … +{extra} more", "dim", enabled=color))
    return lines


def _events_search_json(hits: list[EventSearchHit]) -> list[dict[str, Any]]:
    from imp.engine import events_index as ei
    return [
        {"quest_id": h.quest_id, "title": h.title, "zone": h.zone,
         "last_activity": h.last_activity, "events": ei.events_json(h.events)}
        for h in hits
    ]


def _cmd_events_search(
    args: argparse.Namespace, _control: Path | None = None,
) -> int:
    control = _control if _control is not None else _control_root()
    types = set(args.type) if args.type else None
    agents = set(args.agent) if args.agent else None
    zones = _selected_events_search(args)
    hits = _events_search(control, args.query, zones, types=types, agents=agents)
    if not hits:
        _emit_human([f'0 matches for "{args.query}"'])
        return 0
    color = should_use_color(sys.stdout)
    zone_order = {z: i for i, z in enumerate(_SEARCH_ZONES)}
    hits.sort(key=lambda h: h.last_activity or "", reverse=True)
    hits.sort(key=lambda h: zone_order[h.zone])
    if args.json:
        _page_or_print(
            json.dumps(_events_search_json(hits), indent=2).splitlines(),
            no_pager=args.no_pager)
        return 0
    if args.list:
        lines = [_events_search_list_line(h, query=args.query, color=color)
                 for h in hits]
        _page_or_print(lines, no_pager=args.no_pager, trailing_blank=True)
        return 0
    now = datetime.now(UTC)
    total = sum(h.match_count for h in hits)
    quests = len(hits)
    noun_m = "match" if total == 1 else "matches"
    noun_q = "quest" if quests == 1 else "quests"
    header = pal.ansi(f'{total} {noun_m} in {quests} {noun_q} for "{args.query}"',
                      "dim", enabled=color)
    cap = args.limit if args.limit is not None else 3
    width = _term_width()
    lines = [header]
    for h in hits[:cap]:
        lines.append("")
        lines.extend(_render_events_search_hit(
            h, args.query, now=now, color=color, width=width))
    extra = len(hits) - cap
    if extra > 0:
        lines.append("")
        lines.append(pal.ansi(f"… +{extra} more quests", "dim", enabled=color))
    _page_or_print(lines, no_pager=args.no_pager, trailing_blank=True)
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
    color = should_use_color(sys.stdout)
    explain = getattr(args, "explain", None)
    buf = io.StringIO()
    if explain:
        render_explain(eff, explain, buf, color=color)
    else:
        render_config(eff, buf, color=color, show_all=args.all)
    _emit_human(buf.getvalue().splitlines())
    return 0


_OVERVIEW_COMMANDS: tuple[tuple[str, str], ...] = (
    ("chat", "Open a conversation with imp — ask questions, scout work, or scope a quest."),
    ("new", "Scope a new quest in conversation (optional opening message)."),
    ("resume", "Continue an in-flight quest (the sole resumable one, else a picker)."),
    ("list", "List your quests: in-flight, backlog, and merged."),
    ("show", "Show one quest's detail or a named artifact (imp show <id>)."),
    ("log", "Show a quest's narrative as plain text."),
    ("abandon", "Tear down a quest's worktree, branch, and state."),
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


def _model_arg_type(value: str) -> str:
    """argparse type= for --model: reject malformed provider:model syntax."""
    from imp.providers.model_arg import ModelArgError, parse_model_arg
    try:
        parse_model_arg(value)
    except ModelArgError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    return value


class SplitAppend(argparse.Action):
    """Accumulate a repeatable list option, comma-splitting each value; blanks dropped."""

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str | Iterable[Any] | None,
        option_string: str | None = None,
    ) -> None:
        assert isinstance(values, str)  # each list option takes one value per hit
        current = list(getattr(namespace, self.dest, None) or [])
        current.extend(part.strip() for part in values.split(",") if part.strip())
        setattr(namespace, self.dest, current)


def _cmd_events(args: argparse.Namespace) -> int:
    from imp.engine import events_index as ei

    control = _control_root()
    quest_id: str | None
    token = args.token
    if token is not None:
        # Same comprehension as _resolve_id — picker and resolver agree on candidates.
        matches = [cid for cid in _iter_quest_ids(control) if token in cid]
        if _interactive() and token.strip() and len(matches) >= 2:
            picked = anyio.run(_pick_events_matches, control, matches)
            if isinstance(picked, int):
                return picked
            quest_id = picked
        else:
            try:
                quest_id = _resolve_id(control, token)
            except SystemExit:
                return 2
    elif _interactive():
        picked = anyio.run(_pick_events_target, control)
        if isinstance(picked, int):
            return picked
        quest_id = picked
    else:
        quest_id = _resolve_default_id(control)
    if quest_id is None:
        _emit_human([_NO_QUESTS_MESSAGE])
        return 0
    meta = _unstarted_backlog_meta(control, quest_id)
    if meta is not None:
        _emit_human([f"Quest {quest_id} hasn't started yet — no events to show."])
        return 0
    events = make_store(control).get_events(quest_id)
    if events is None:
        print(f"imp: no such quest {quest_id!r}", file=sys.stderr)
        return 2

    indexed = list(enumerate(events, start=1))
    handle_map = ei.subagent_handle_map(indexed)
    max_ord = indexed[-1][0] if indexed else 0

    types = set(args.type) if args.type else None
    if args.wire:
        types = (types or set()) | {"user_message", "engine_message"}
    subtypes = set(args.subtype) if args.subtype else None
    agents = set(args.agent) if args.agent else None
    steps = set(args.step) if args.step else None
    range_specs = list(args.range) if args.range else []
    if args.range_pos:
        range_specs.append(args.range_pos)
    intervals: list[tuple[int, int]] = []
    try:
        for spec in range_specs:
            intervals.extend(ei.parse_range_spec(spec, max_ord))
    except ei.RangeSpecError as exc:
        print(f"imp: {exc}", file=sys.stderr)
        return 2

    if steps is not None:
        known = {s.name for s in ei.step_windows(indexed)}
        missing = sorted(steps - known)
        if missing:
            print(f"imp: no step {missing[0]!r} in {quest_id}", file=sys.stderr)
            return 2

    leaf = bool(types or subtypes or agents or intervals or args.all
                or args.limit is not None)

    if not leaf and steps is not None:
        scoped_all = []
        for name in sorted(steps):
            scoped = ei.build_step_index(indexed, name, handle_map)
            assert scoped is not None  # validated in the up-front step check
            scoped_all.append(scoped)
        if args.json:
            payload = (ei.step_index_json(scoped_all[0]) if len(scoped_all) == 1
                       else [ei.step_index_json(s) for s in scoped_all])
            _page_or_print(json.dumps(payload, indent=2).splitlines(),
                           no_pager=args.no_pager)
            return 0
        lines = []
        for i, scoped in enumerate(scoped_all):
            if i:
                lines.append("")
            lines.extend(ei.render_step_index(scoped))
        _page_or_print(lines, no_pager=args.no_pager, trailing_blank=True)
        return 0

    if not leaf:
        index = ei.build_index(indexed, quest_id)
        if args.json:
            _page_or_print(
                json.dumps(ei.index_json(index), indent=2).splitlines(),
                no_pager=args.no_pager)
            return 0
        _page_or_print(ei.render_index(index), no_pager=args.no_pager,
                       trailing_blank=True)
        return 0

    selected = ei.select_event_lines(
        indexed, types=types, subtypes=subtypes, agents=agents,
        ranges=intervals or None, step_names=steps,
        all_=args.all, limit=args.limit, handle_map=handle_map)
    if args.json:
        _page_or_print(
            json.dumps(ei.events_json(selected), indent=2).splitlines(),
            no_pager=args.no_pager)
        return 0
    width = None if args.full else _term_width()
    _page_or_print(
        [ei.digest_line(o, ev, handle_map, width=width) for o, ev in selected],
        no_pager=args.no_pager, trailing_blank=True)
    return 0


def _build_events_search_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="imp events search",
        description=(
            "Case-insensitive substring search over structured event content "
            "(tool calls, tool results, agent reasoning, skill loads) across "
            "quests — the structured-event sibling of `imp search` (prose only). "
            "Searches active, backlog, and archive by default; --type / --agent "
            "narrow which events match."),
    )
    p.add_argument(
        "query", metavar="QUERY",
        help="Substring matched case-insensitively against event content.")
    p.add_argument("--active", action="store_true",
                   help="Search in-flight quests' events.")
    p.add_argument("--backlog", action="store_true",
                   help="Search backlog quests (matched on title + intent).")
    p.add_argument("--archived", action="store_true",
                   help="Search archived quests' events.")
    p.add_argument(
        "--type", action=SplitAppend, default=None,
        help="Keep only these event types (repeatable / comma-joined).")
    p.add_argument(
        "--agent", action=SplitAppend, default=None,
        help="Keep only these authors: step name, subagent handle, or sub:<id> "
             "(repeatable / comma-joined).")
    p.add_argument("--limit", type=int, default=None,
                   help="Raise the matched-quest cap above the default 3.")
    p.add_argument("-l", "--list", action="store_true", dest="list",
                   help="One terse line per matched quest (id, title, zone).")
    p.add_argument("--json", action="store_true",
                   help="Structured output of matched events.")
    p.add_argument("--no-pager", action="store_true",
                   help="Disable the pager even when stdout is a TTY.")
    p.set_defaults(func=_cmd_events_search)
    return p


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
        "new", help="Start a new quest: run the workflow from an opening intent.")
    p_new.add_argument(
        "opening", nargs="?", metavar="MESSAGE",
        help="Opening intent handed verbatim to the workflow's opening step "
             "as its first message; omit to start with an interactive prompt. "
             "Distinct from --message, which is a headless reply to the first solicit.")
    p_new.add_argument("--title", metavar="TEXT", default=None,
                       help="Set the quest title explicitly; the workflow's "
                            "opening step names the quest if omitted.")
    p_new.add_argument("--non-interactive", action="store_true",
                       help="Run headless: no live prompt; supply the first reply "
                            "via --message.")
    p_new.add_argument("--autonomous", action="store_true",
                       help="Auto-take the recommendation at recommendation-bearing "
                            "gates instead of confirming each; genuine intent-forks "
                            "still pause for a human. Independent of --non-interactive.")
    p_new.add_argument("--message", metavar="TEXT", default=None,
                       help="Headless reply consumed by the first bare-turn-end prompt; "
                            "pause then resume with another --message. "
                            "Requires --non-interactive.")
    p_new.add_argument("--backlog", metavar="TITLE", default=None, dest="idea",
                       help="File an idea for your backlog with this TITLE and "
                            "exit immediately without running (the positional "
                            "MESSAGE is an optional longer brief). Start it later "
                            "with `imp resume <id>`.")
    p_new.add_argument("--depends-on", metavar="ID", action="append",
                       default=None, dest="depends_on",
                       help="A quest id this backlog quest depends on "
                            "(repeatable). Gates ready/blocked; never changes the "
                            "base branch.")
    p_new.add_argument("--workflow", metavar="NAME", default=None,
                       help="Run this quest on workflow NAME instead of the "
                            "project default. A bundled name needs no commit; a "
                            "committed imp/workflows/NAME fork overrides it.")
    p_new.add_argument("--base", metavar="BRANCH", default=None,
                       help="Base the quest on an existing BRANCH instead of "
                            "the configured default-base. BRANCH must already "
                            "exist.")
    p_new.add_argument("--model", metavar="ARG", default=None, type=_model_arg_type,
                       help="Run the opening step under a chosen model "
                            "(provider:model or bare model); the same override as "
                            "typing /model at the opening prompt.")
    p_new.set_defaults(func=_cmd_new)

    p_chat = sub.add_parser(
        "chat",
        help="Open a conversation with imp (ask questions, scout work, or "
             "scope a quest).")
    p_chat.add_argument(
        "opening", nargs="?", metavar="MESSAGE",
        help="Optional opening message; omit to start with an interactive prompt.")
    p_chat.add_argument("--non-interactive", action="store_true",
                        help="Headless: no live prompt; supply the reply via --message.")
    p_chat.add_argument("--autonomous", action="store_true",
                        help="Auto-take the recommendation at recommendation-bearing "
                             "gates instead of confirming each; genuine intent-forks "
                             "still pause for a human. Independent of --non-interactive.")
    p_chat.add_argument("--message", metavar="TEXT", default=None,
                        help="Headless reply consumed by the first bare-turn-end prompt; "
                             "requires --non-interactive.")
    p_chat.add_argument("--model", metavar="ARG", default=None, type=_model_arg_type,
                        help="Run the opening step under a chosen model "
                             "(provider:model or bare model); the same override as "
                             "typing /model at the opening prompt.")
    p_chat.set_defaults(func=_cmd_chat)

    p_resume = sub.add_parser(
        "resume",
        help="Resume a paused or interrupted quest by id or token.",
        description=(
            "Resume a paused or interrupted quest, replaying its narrative and "
            "continuing the workflow where it stopped. With no token on a tty, "
            "pick from a list of resumable quests."))
    p_resume.add_argument(
        "token", nargs="?",
        help="Quest id or resume token identifying which quest to resume.")
    p_resume.add_argument("--non-interactive", action="store_true",
                          help="Headless: no live prompt; supply the reply via --message.")
    p_resume.add_argument("--autonomous", action="store_true",
                          help="Auto-take the recommendation at recommendation-bearing "
                               "gates instead of confirming each; genuine intent-forks "
                               "still pause for a human. Independent of --non-interactive.")
    p_resume.add_argument("--message", metavar="TEXT", default=None,
                          help="Headless reply consumed by the first bare-turn-end prompt; "
                               "pause+resume with another --message. "
                               "Requires --non-interactive.")
    p_resume.set_defaults(func=_cmd_resume)

    p_log = sub.add_parser(
        "log",
        help="Show the narrative for a quest.",
        description=(
            "Show the narrative for a quest as plain text. --full expands "
            "every fold; --show/--hide toggle individual content categories "
            "(thinking, tools, subagents, prompts, tiers) over the lean view. "
            "Set NO_COLOR=1 to disable colors. "
            "Tip: imp log <id> --full | grep ... for line-oriented scripting."
        ),
    )
    p_log.add_argument(
        "token", nargs="?",
        help="Quest id or its short token (omit for the most recent quest).")
    p_log.add_argument(
        "range_pos", nargs="?", default=None, metavar="RANGE",
        help="Ordinal selector, e.g. 42, 59-120, 59-, -80 (like imp events).")
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
    p_log.add_argument(
        "-f", "--follow", action="store_true",
        help="Live-tail the narrative (tail -f): replay it, then stream new "
             "events until the quest reaches a terminal status. Renders the "
             "live run view; implies --no-pager.",
    )
    p_log.add_argument(
        "--show", action=SplitAppend, default=None, metavar="CAT",
        help="Reveal a content category over the lean view: thinking, tools, "
             "subagents, prompts, tiers (repeatable / comma-joined).")
    p_log.add_argument(
        "--hide", action=SplitAppend, default=None, metavar="CAT",
        help="Hide a content category (repeatable / comma-joined).")
    p_log.add_argument(
        "--step", action=SplitAppend, default=None,
        help="Scope to a step's window (repeatable / comma-joined).")
    p_log.add_argument(
        "--range", action=SplitAppend, default=None, dest="range",
        help="Ordinal selector (repeatable / comma-joined).")
    p_log.add_argument(
        "--agent", action=SplitAppend, default=None,
        help="Keep only these authors: step name, subagent handle, or "
             "sub:<id> (repeatable / comma-joined).")
    p_log.add_argument(
        "--limit", type=int, default=None,
        help="Keep only the last N events.")
    p_log_export = p_log.add_mutually_exclusive_group()
    p_log_export.add_argument(
        "--html", nargs="?", metavar="FILE", const=_LOG_EXPORT_DEFAULT,
        default=None,
        help="Export the narrative as a self-contained HTML file "
             "(FILE, or imp-log-<id>.html when omitted; '-' streams to stdout).",
    )
    p_log_export.add_argument(
        "--md", nargs="?", metavar="FILE", const=_LOG_EXPORT_DEFAULT,
        default=None,
        help="Export the narrative as a Markdown file "
             "(FILE, or imp-log-<id>.md when omitted; '-' streams to stdout).",
    )
    p_log.set_defaults(func=_cmd_log)

    p_events = sub.add_parser(
        "events",
        help="Slice and dice a quest's event log (imp events <id>).",
        description=(
            "Index and filter a quest's event log. Bare: a steps+types index. "
            "--step <name>: a scoped index for that step's window. "
            "--type / --agent / <range> / --all: compact event lines "
            "(#N ordinal, type, [agent], summary). --json for structured "
            "output. Omit <id> for the most recent quest."
        ),
    )
    p_events.add_argument(
        "token", nargs="?", default=None,
        help="Quest id or short token (omit for the most recent quest).")
    p_events.add_argument(
        "range_pos", nargs="?", default=None, metavar="RANGE",
        help="Ordinal selector, e.g. 42, 59-120,210-255, 59-, -80.")
    p_events.add_argument(
        "--step", action=SplitAppend, default=None,
        help="Scope to a step's window (repeatable / comma-joined).")
    p_events.add_argument(
        "--type", action=SplitAppend, default=None,
        help="Keep only these event types (repeatable / comma-joined).")
    p_events.add_argument(
        "--subtype", action=SplitAppend, default=None,
        help="Keep only these message subtypes, e.g. system_prompt, intent, "
             "continuation (repeatable / comma-joined).")
    p_events.add_argument(
        "--wire", action="store_true",
        help="Shorthand for --type user_message,engine_message (everything that "
             "went out on the wire).")
    p_events.add_argument(
        "--agent", action=SplitAppend, default=None,
        help="Keep only these authors: step name, subagent handle, or "
             "sub:<id> (repeatable / comma-joined).")
    p_events.add_argument(
        "--range", action=SplitAppend, default=None, dest="range",
        help="Ordinal selector (repeatable / comma-joined).")
    p_events.add_argument(
        "--limit", type=int, default=None,
        help="Keep only the last N event lines.")
    p_events.add_argument(
        "--all", action="store_true",
        help="All event lines (escape hatch from the index/filters).")
    p_events.add_argument(
        "--full", action="store_true",
        help="Render full untruncated event lines (lift the one-terminal-row "
             "width clip); composes with every filter.")
    p_events.add_argument(
        "--json", action="store_true",
        help="Structured output instead of text.")
    p_events.add_argument(
        "--no-pager", action="store_true",
        help="Disable the pager even when stdout is a TTY.")
    p_events.set_defaults(func=_cmd_events)

    p_list = sub.add_parser(
        "list",
        help="List your quests: in-flight, backlog, and merged.",
        description=(
            "With no argument, an overview of your quests — in-flight, the "
            "backlog, and every merged quest. -a/--all also includes "
            "abandoned/shelved quests (the block is then labelled `history`)."),
    )
    p_list.add_argument(
        "-a", "--all", action="store_true",
        help="Also include abandoned and shelved quests "
             "(the block is then labelled `history`).")
    p_list.add_argument(
        "--active", action="store_true",
        help="Show only in-flight quests and drafts.")
    p_list.add_argument(
        "--backlog", action="store_true",
        help="Show only backlog items.")
    p_list.add_argument(
        "--merged", action="store_true",
        help="Show only merged quests.")
    p_list.add_argument(
        "--abandoned", action="store_true",
        help="Show only abandoned quests.")
    p_list.add_argument(
        "--shelved", action="store_true",
        help="Show only shelved conversations.")
    p_list.add_argument(
        "--archived", action="store_true",
        help="Show merged, abandoned, and shelved quests (the archive).")
    p_list.add_argument(
        "--since", metavar="SPEC", default=None,
        help="Show only quests active since SPEC — today, yesterday, this-week, "
             "this-month, or a number + unit h/d/w/m/y (e.g. 2d, 1w).")
    p_list.add_argument(
        "--no-pager", action="store_true",
        help="Disable the pager even when stdout is a TTY.")
    p_list.set_defaults(func=_cmd_list)

    p_show = sub.add_parser(
        "show",
        help="Show one quest's detail or a named artifact (imp show <id>).",
        description=(
            "Print a quest's detail card, or name the spec artifact to print "
            "just that. "
            "Reads the live worktree files while a quest is in flight and "
            "falls back to the archive once it is merged."),
    )
    p_show.add_argument(
        "token", nargs="?", default=None,
        help="Quest id or its short token.")
    p_show.add_argument(
        "resource", nargs="?", default=None,
        choices=["spec"],
        help="Which artifact to print (default: a detail card).")
    p_show.add_argument(
        "--no-pager", action="store_true",
        help="Disable the pager even when stdout is a TTY.")
    p_show.add_argument(
        "--json", action="store_true",
        help="Emit the overview detail card as a structured JSON object.")
    p_show.set_defaults(func=_cmd_show)

    p_search = sub.add_parser(
        "search",
        help="Search quests by id, title, or conversation content.",
        description=(
            "Search active (in-flight + drafts), backlog, and archived quests "
            "for the query as a case-insensitive substring of a quest's id, "
            "title, or conversation content. With no zone flag, all three zones "
            "are searched. --merged scopes to merged quests."),
    )
    p_search.add_argument(
        "query", metavar="QUERY",
        help="Substring matched case-insensitively against id, title, and content.")
    p_search.add_argument(
        "--active", action="store_true",
        help="Search in-flight and draft quests.")
    p_search.add_argument(
        "--backlog", action="store_true",
        help="Search backlog quests.")
    p_search.add_argument(
        "--merged", action="store_true",
        help="Search merged quests.")
    p_search.add_argument(
        "--abandoned", action="store_true",
        help="Search abandoned quests.")
    p_search.add_argument(
        "--shelved", action="store_true",
        help="Search shelved conversations.")
    p_search.add_argument(
        "--archived", action="store_true",
        help="Search archived (merged, abandoned, shelved) quests.")
    p_search.add_argument(
        "--merged-after", metavar="COMMIT", default=None,
        help="Keep only merged quests whose merge landed after COMMIT "
             "(implies --merged).")
    p_search.add_argument(
        "-l", "--list", action="store_true", dest="list",
        help="One terse line per quest (id, title, zone); no excerpts.")
    p_search.add_argument(
        "--no-pager", action="store_true",
        help="Disable the pager even when stdout is a TTY.")
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
        help="Tear down a quest's worktree, branch, and state (logged).",
        description=(
            "Tear down a materialized quest — its worktree, branch, and state — "
            "or discard a pure backlog idea. With no token, pick from a list."),
    )
    p_abandon.add_argument(
        "token", nargs="?",
        help="Quest id or short token identifying which quest to abandon "
             "(omit on a tty to pick from a list).")
    p_abandon.set_defaults(func=_cmd_abandon)

    p_edit = sub.add_parser(
        "edit",
        help="Edit a quest's title, backlog membership, dependencies, or opening intent.",
        description=(
            "Edit a quest's title, backlog membership, dependencies, or append to "
            "its opening intent. Zone moves (--backlog/--active) apply to "
            "un-materialized quests only."))
    p_edit.add_argument("token", help="Quest id or its short token.")
    p_edit.add_argument("--title", metavar="TEXT", default=None,
                        help="Set the quest title.")
    p_edit.add_argument(
        "--append-intent", metavar="TEXT", default=None,
        help="Append a verbatim note to an unstarted quest's opening intent.")
    p_edit_zone = p_edit.add_mutually_exclusive_group()
    p_edit_zone.add_argument(
        "--backlog", action="store_true",
        help="Move an un-materialized quest to the backlog zone (origin=backlog).")
    p_edit_zone.add_argument(
        "--active", action="store_true",
        help="Return an un-materialized quest to the Active zone (origin=draft).")
    p_edit_deps = p_edit.add_mutually_exclusive_group()
    p_edit_deps.add_argument(
        "--depends-on", metavar="ID", action="append", default=None,
        dest="depends_on", help="Replace the dependency list (repeatable).")
    p_edit_deps.add_argument(
        "--clear-deps", action="store_true",
        help="Empty the dependency list.")
    p_edit.set_defaults(func=_cmd_edit)

    p_rc = sub.add_parser(
        "remote-control",
        help="Run the optional per-repo daemon that relays all quests to one transport.",
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
        _emit_human(render_overview(should_use_color(sys.stdout)).splitlines())
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
        _emit_human(render_overview(should_use_color(sys.stdout)).splitlines())
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
    the live streaming output. We pre-empt that by owning root logging here, before the
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
    with contextlib.suppress(Exception):
        install_stack_dumper(
            _control_root() / ".imp" / f"cli.{os.getpid()}.stack-dump.txt")
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
        if len(args) >= 2 and args[0] == "events" and args[1] == "search":
            ns = _build_events_search_parser().parse_args(args[2:])
        else:
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
