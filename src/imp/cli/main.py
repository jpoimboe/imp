# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""imp CLI entrypoint.

Plan 1: minimal commands (new, resume, log, status). No streaming, no signals,
no spinner. Plan 3 adds the polished UX layer.
"""

from __future__ import annotations

import argparse
import contextlib
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
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, NoReturn

from imp import __version__
from imp.cli import palette as pal
from imp.cli import quest_row, splash
from imp.cli.exit_codes import ExitCode
from imp.cli.help_format import (
    TAGLINE,
    ImpParser,
    ImpRawHelpFormatter,
    ImpTopLevelHelpFormatter,
)
from imp.cli.narrative import MANUAL_STOP_LABELS
from imp.cli.palette import ansi, should_use_color
from imp.cli.prompt import DEFAULT_HINT, OPENING_HINTS, Prompt
from imp.paths import project_state_dir
from imp.remote.session.protocol import (
    CONTROL_ABANDON,
    CONTROL_ARCHIVE,
    CONTROL_DISCARD,
    CONTROL_SAVE,
    QUERY_USER_RENAME,
)

if TYPE_CHECKING:
    from imp.cli.live_driver import LiveDriver
    from imp.cli.live_status import LiveStatus
    from imp.cli.narrative import NarrativeTree
    from imp.cli.quota_column import QuotaColumn, QuotaFallback
    from imp.cli.render_summary import StepRow, SummaryHeader
    from imp.cli.store import QuestCard, SearchHit, SearchZones, Store, Zone, _Row
    from imp.cli.terminal_watchdog import WatchedStream
    from imp.engine.quest import Quest
    from imp.engine.state import State
    from imp.remote.session.attach import DaemonStartupError


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
    """Resolve the opening seed for `imp new`/`imp chat`.

    The positional MESSAGE and --message are interchangeable aliases: either,
    when non-empty, becomes the user's verbatim opening intent (delivered
    unwrapped). Whitespace-only values are treated as absent. If both are
    non-empty, raise SeedError. A spec is named in the opening message and read
    by the workflow's opening step itself, never inlined here."""
    title = getattr(args, "title", None)
    opening = getattr(args, "opening", None)
    message = getattr(args, "message", None)

    opening = opening if (opening and opening.strip()) else None
    message = message if (message and message.strip()) else None

    if opening is not None and message is not None:
        raise SeedError(
            "Pass the opening message once — as the positional argument or "
            "--message, not both.")

    return SeedResolution(title, opening or message)


# -- end-of-run status lines ------------------------------------------------
# Every terminal line shares one spine: a gold quest id, a middle display-name
# segment, and a dot-joined gold outcome tail; only an error block's head
# (_error_block) is red. The middle segment is always present via the shared
# resolver (quest_row.resolve_display_name): a real title is plain, else the
# opening message renders dim, else the dim `(untitled)` placeholder. Dim is
# color-only, so NO_COLOR / non-TTY output stays plain and script-parseable.
# Shared by `new`/`resume`, leaves, and the completed block.

def _spine_name(title: str, intent: str | None, *,
                color: bool) -> tuple[str, quest_row.NameKind]:
    """The styled middle name segment and its kind, resolved title→intent→(untitled)."""
    resolved = quest_row.resolve_display_name(title, intent)
    name = quest_row._truncate(resolved.text, quest_row.TITLE_CAP)
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


@dataclass(frozen=True)
class Merge:
    """Where a run's merge landed: the base branch and the merge commit's full sha."""
    base: str
    commit: str


def _emit_completed(quest: Quest, *, merge: Merge | None,
                    intent: str | None = None,
                    commits: Sequence[dict[str, str]] = ()) -> None:
    """The clean-finish block on the shared status spine: a gold
    `merged into <base> as <sha8>` / `completed · commits on imp/<id>` tail, then the
    run's commits indented beneath it, oldest-first when any were recorded; one trailing
    blank. `merge` is None when the run completed without merging."""
    color = should_use_color(sys.stdout)
    intent = quest_row.fallback_intent(intent, quest.seed_opening)
    tail = (f"merged into {merge.base} as {merge.commit[:8]}" if merge is not None
            else f"completed · commits on imp/{quest.id}")
    print(_status_spine(quest.id, quest.title, tail, "wordmark", color=color,
                        intent=intent))
    for ln in _commit_lines(commits, color=color):
        print(ln)
    print()


# Single source: error pause-reason base token -> user-facing label; covers all _POLICY
# except context_window, a clean _PAUSE_QUALIFIER pause.
_ERROR_LABELS: dict[str, str] = {
    "terminated": "agent error",
    "internal_error": "internal error",
    "api": "API error",
    "quota": "quota exceeded",
    "config": "config error",
    "content_filter": "content filtered",
    "loop": "loop detected",
    "unknown": "unknown error",
    "retry_ceiling": "retries exhausted",
    "reauth_needed": "auth failed",
    "auth": "auth failed",
    "auth_terminal": "auth failed",
    "rate_limit": "rate limited",
    "network": "network error",
    "stalled": "stalled",
    "agent_output_missing": "no output",
    "crash": "crash",
}

# Pause trigger -> its banner display form. Internal reason strings stay
# lowercase; only the banner display is transformed.
_PAUSE_QUALIFIER: dict[str, str] = {
    **MANUAL_STOP_LABELS,
    "max_loops": "loop limit",
    "context_window": "context handoff limit",
}


def _identity_line(quest_id: str, title: str, *, color: bool,
                   intent: str | None = None) -> str:
    """The shared identity line: gold id, ` · `, then the resolved name — the title,
    else the dim opening-prompt fallback; a bare gold id only when the quest has
    neither (kind UNTITLED)."""
    ident = ansi(quest_id, *pal.NODE_COLORS["quest_id"], enabled=color)
    name, kind = _spine_name(title, intent, color=color)
    if kind is not quest_row.NameKind.UNTITLED:
        ident = f"{ident} · {name}"
    return ident


def _clean_block(head: str, quest_id: str, title: str, *, color: bool,
                 intent: str | None = None) -> list[str]:
    """The clean-exit sibling of `_error_block`: a gold `head` line on its own line, then
    the identity line. No resume line — pause callers append their own resume line."""
    return [ansi(head, "wordmark", enabled=color),
            _identity_line(quest_id, title, color=color, intent=intent)]


def _commit_lines(commits: Sequence[dict[str, str]], *, color: bool) -> list[str]:
    """One `  <sha8>  <subject>` line per recorded commit, oldest first, indented two
    spaces: gray sha8, subject in the default fg. The subject is content-origin text, so
    it is caret-escaped before any SGR is wrapped around the line."""
    from imp.cli.render_text import _caret_escape
    return ["  " + ansi(c["sha"][:8], "gray", enabled=color) + "  "
            + _caret_escape(c["subject"])
            for c in commits]


def _error_block(head: str, body: list[str], quest_id: str, title: str,
                 *, color: bool, intent: str | None = None) -> list[str]:
    """The shared three-part error block: a red `head` line, `body` lines in the
    default fg, then the identity line (gold id, ` · `, then the resolved name —
    the title, else the dim opening-prompt fallback; a bare gold id only when the
    quest has neither). No resume line."""
    lines = [ansi(head, "red", enabled=color)]
    lines.extend(body)
    lines.append(_identity_line(quest_id, title, color=color, intent=intent))
    return lines


@dataclass(frozen=True)
class _PauseInfo:
    """A terminal pause's structured reason: the base token (`api`, `terminated`,
    `interrupt`, a deliberate-leave trigger, `crash`, …) and the full cause text
    (multi-line allowed), or None when the reason carries no cause."""
    base: str
    cause: str | None


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
    if info is not None and info.base in _PAUSE_QUALIFIER:
        display = _PAUSE_QUALIFIER[info.base]
        head_line = (ansi("paused", "wordmark", enabled=color)
                     + ansi(f" · {display}", "dim", enabled=color))
        return [
            head_line,
            _identity_line(name, title, color=color, intent=intent),
            _resume_line(token),
        ]
    head = info.base if info is not None else "paused"
    return [
        *_clean_block(head, name, title, color=color, intent=intent),
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


def _emit_paused_summary(quest: Quest, *, info: _PauseInfo | None,
                         intent: str | None = None) -> None:
    """The trailing run-status line on a non-interactive pause; `info` None renders the
    bare `paused` block."""
    color = should_use_color(sys.stdout)
    intent = quest_row.fallback_intent(intent, quest.seed_opening)
    for ln in _pause_lines(quest.id, quest.id, info=info, title=quest.title,
                           color=color, intent=intent):
        print(ln)
    print()


def _emit_leave_pause(quest: Quest, trigger: str, *,
                      intent: str | None = None) -> None:
    """A deliberate leave's own block: the trigger-qualified pause block."""
    _emit_paused_summary(quest, info=_PauseInfo(trigger, None), intent=intent)


def _emit_cli_error(message: str) -> None:
    """Non-quest CLI error sink: writes `imp: <message>` to stderr."""
    print(f"imp: {message}", file=sys.stderr)


def _emit_merge_blocked(
    quest: Quest, reason: str | None, detail: str | None = None,
    note: str | None = None, *, intent: str | None = None,
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
    intent = quest_row.fallback_intent(intent, quest.seed_opening)
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
                f"`{quest.project_root}` can be removed)."]
    else:
        summary = "couldn't merge"
        body = [f"Resolve, then `imp resume {cid}`."]
    if note:
        body.append(note)
    if detail and reason in ("refused", "conflict"):
        body.append(f"git said: {detail.strip()}")
    for ln in _error_block(f"merge blocked: {summary}", body, cid, quest.title,
                           color=color, intent=intent):
        print(ln)
    print()


def _emit_startup_failed(quest: Quest, error: DaemonStartupError | None) -> None:
    """Session daemon died before binding its socket — render the shared three-part
    error block on stdout (red head + body + gold identity line), matching every
    other terminal exit block, then one trailing blank. Built from
    DaemonStartupError's structured fields, not its message string; degrades to a
    backstop head+body when no error detail is available (the timeout path)."""
    from imp.cli.store import _resolve_intent
    color = should_use_color(sys.stdout)
    if error is not None:
        if error.exit_code is not None:
            head = (f"startup failed: session daemon exited with code "
                    f"{error.exit_code} before it came up")
        else:
            head = "startup failed: session daemon exited before it came up"
        body = error.stderr_tail.splitlines() if error.stderr_tail else []
        body.append(f"see {error.stderr_path}")
    else:
        head = "startup failed: session daemon did not come up"
        body = [f"It may have crashed; see "
                f"{project_state_dir(quest.control_root) / 'remote-control.log'}"]
    intent = quest_row.fallback_intent(
        _resolve_intent(list(quest.event_log().read())), quest.seed_opening)
    for ln in _error_block(head, body, quest.id, quest.title,
                           color=color, intent=intent):
        print(ln)
    print()


def _emit_lost_connection(quest: Quest, *, intent: str | None = None) -> None:
    """The client's event stream faulted: report the ending it saw, never a state it didn't."""
    color = should_use_color(sys.stdout)
    intent = quest_row.fallback_intent(intent, quest.seed_opening)
    body = [f"It may still be running — `imp resume {quest.id}` to reattach."]
    for ln in _error_block("lost the connection to the run", body, quest.id,
                           quest.title, color=color, intent=intent):
        print(ln)
    print()


def _recorded_commits(events: list[dict[str, Any]], *,
                      merged: bool) -> list[dict[str, str]]:
    """The commit list the engine recorded on the ending this run actually took.
    Keyed on the same `merged` the headline is, so the body can never describe the
    other ending; `[]` when the event predates the field or never landed."""
    wanted = "quest_merged" if merged else "workflow_completed"
    for ev in reversed(events):
        if ev.get("type") == wanted:
            payload: dict[str, Any] = ev.get("payload") or {}
            commits: list[dict[str, str]] = payload.get("commits", [])
            return commits
    return []


def _emit_daemon_died(quest: Quest, *, intent: str | None = None) -> None:
    """The run's stream ended carrying no outcome: a traceback in daemon.stderr names
    the crash, otherwise the run simply stopped reporting. No resume line — the inline
    hint is the remediation."""
    color = should_use_color(sys.stdout)
    intent = quest_row.fallback_intent(intent, quest.seed_opening)
    stderr_path = quest.quest_dir / "daemon.stderr"
    body: list[str] = []
    if _daemon_stderr_has_crash(stderr_path):
        head = f"crash: see {stderr_path}"
    else:
        head = "run ended without reporting an outcome"
        body = [f"It may not have finished — `imp resume {quest.id}` to check."]
    for ln in _error_block(head, body, quest.id, quest.title,
                           color=color, intent=intent):
        print(ln)
    print()


_LEAVE_TAILS: dict[str, str] = {
    CONTROL_SAVE: "paused",
    CONTROL_ARCHIVE: "archived",
    CONTROL_DISCARD: "discarded",
    CONTROL_ABANDON: "abandoned",
}


def _emit_leave_block(
    quest: Quest, disposition: str, *, intent: str | None, removed: str | None = None,
) -> None:
    """The closing block for a leave the daemon applies, whichever client sent it."""
    color = should_use_color(sys.stdout)
    intent = quest_row.fallback_intent(intent, quest.seed_opening)
    print(_status_spine(quest.id, quest.title, _LEAVE_TAILS[disposition], "wordmark",
                        color=color, intent=intent))
    if disposition == CONTROL_SAVE:
        print(_resume_line(quest.id))
    elif disposition == CONTROL_ARCHIVE:
        print(_resume_line(quest.id, " — or find it later with imp search"))
    elif disposition == CONTROL_ABANDON and removed is not None:
        print(f"removed: {removed}")
    print()


def _print_leave_disposition(
    quest: Quest, leave_state: dict[str, str], *, intent: str | None = None,
) -> ExitCode | None:
    """Print the optimistic leave message for a daemon-applied disposition.

    The disposition itself is applied by the daemon after it releases the lock;
    here we only echo what was (or is being) done, on the shared spine. Returns
    USER_ENDED when a leave was handled, None to fall through to the next row.
    """
    if leave_state.get("intent") == "abandon":
        _emit_leave_block(quest, CONTROL_ABANDON, intent=intent,
                          removed=leave_state.get("removed"))
        return ExitCode.USER_ENDED
    disposition = leave_state.get("disposition")
    if disposition is not None:
        _emit_leave_block(quest, disposition, intent=intent)
        return ExitCode.USER_ENDED
    if leave_state.get("send_failed"):
        color = should_use_color(sys.stdout)
        intent = quest_row.fallback_intent(intent, quest.seed_opening)
        print(_status_spine(quest.id, quest.title, "left running", "wordmark",
                            color=color, intent=intent))
        print(_resume_line(quest.id))
        print()
        return ExitCode.USER_ENDED
    return None


def _project_root() -> Path:
    from imp.engine.project_root import find_project_root
    return find_project_root()


def _control_root() -> Path:
    from imp.engine import worktree
    return worktree.find_control_root(Path.cwd())


def _settings_root() -> Path:
    """The one root every settings layer is read from and written to: the main
    checkout, resolved via git even from inside a quest worktree. Falls back to
    the marker walk only when git can resolve nothing (no repo, no git binary) —
    there the two roots coincide anyway, and a command must not die over it."""
    try:
        return _control_root()
    except Exception:
        return _project_root()


def _claiming_settings_root() -> Path | None:
    """`_settings_root()` when it is a real project, else None. Resolving a state
    directory CLAIMS a slug, and `_settings_root` falls back to the bare cwd
    outside a project — which would squat a slug on every `imp` run anywhere."""
    root = _settings_root()
    return root if (root / "imp" / "config.yaml").is_file() else None


def _preflight_imp_init(*, control_root: Path) -> str | None:
    """Relocates and dresses the ProviderUnavailable that per-step resolution
    would otherwise raise deep in a step."""
    from imp.engine.errors import LoadError, LoadErrors
    from imp.providers import auth_hint_lines
    from imp.providers.effective import load_effective_config
    from imp.providers.resolve import UnknownProvider, _first_available_provider
    try:
        config = load_effective_config(control_root=control_root).config
        no_provider = _first_available_provider(config) is None
    except (LoadError, LoadErrors, UnknownProvider):
        # Malformed config or unknown provider — let resolution surface it with full context.
        return None
    if not no_provider:
        return None
    names = list(config.providers)
    if not names:
        return "no providers configured — run 'imp config set providers claude'"
    return "\n".join([
        f"no authenticated provider — configured: {', '.join(names)}",
        *auth_hint_lines(names),
    ])


def _apply_theme() -> None:
    """Pick the active color theme once, before any command renders. `IMP_THEME`
    overrides everything; otherwise the value detected and persisted at `imp init`
    (config.theme); otherwise the universal `ansi`. Best-effort — a missing or
    unreadable config must never fail a command over theming, it just stays ansi."""
    override = pal.theme_override()
    if override is not None:
        pal.set_theme(override)
        return
    from imp.providers.effective import load_effective_config
    try:
        theme = load_effective_config(
            control_root=_claiming_settings_root()).config.theme
        pal.set_theme(theme)
    except Exception:
        pass


def _init_input(prompt: str) -> str:
    try:
        return input(prompt)
    except KeyboardInterrupt:
        print(file=sys.stderr)
        raise


def _cmd_init(args: argparse.Namespace) -> int:
    from imp.assets import selectable_workflow_names
    from imp.cli.config_write import ConfigWriteError
    from imp.cli.init import (
        GitIdentityUnavailable,
        IntegrationBranchUnresolved,
        InvalidProvidersFlag,
        parse_providers_flag,
        run_init,
        run_init_noninteractive,
    )
    from imp.engine import worktree
    project = _project_root()
    if worktree.head_detached(project):
        _emit_cli_error(
            "HEAD is detached; check out a branch before `imp init` "
            "(it commits imp's config).")
        return 2
    wf_arg = getattr(args, "workflow", None)
    if wf_arg is not None:
        names = selectable_workflow_names(project)
        if wf_arg not in names:
            _emit_cli_error(
                f"unknown workflow {wf_arg!r}; available: {', '.join(names)}")
            return 2
    providers_arg = getattr(args, "providers", None)
    providers_override: list[str] | None = None
    if providers_arg is not None:
        try:
            providers_override = parse_providers_flag(providers_arg)
        except InvalidProvidersFlag as e:
            _emit_cli_error(f"{e}")
            return 2
    headless, auto_headless = _select_headless(args)
    try:
        if headless:
            if auto_headless and not (project / "imp" / "config.yaml").is_file():
                print(
                    "No interactive terminal detected — writing defaults to "
                    "imp/config.yaml. Override with --workflow / --base / "
                    "--pythonpath / --providers.",
                    file=sys.stderr,
                )
            raw_pythonpath = getattr(args, "pythonpath", None)
            pythonpath: list[str] | Literal[False] | None
            if raw_pythonpath is None:
                pythonpath = None
            else:
                pythonpath = [s.strip() for s in raw_pythonpath.split(",")
                              if s.strip()] or False
            run_init_noninteractive(
                project,
                output_fn=print,
                workflow=getattr(args, "workflow", None),
                base=getattr(args, "base", None),
                pythonpath=pythonpath,
                providers_override=providers_override,
            )
        else:
            run_init(project, input_fn=_init_input, output_fn=print,
                     providers_override=providers_override,
                     settings_root=_claiming_settings_root())
    except (IntegrationBranchUnresolved, GitIdentityUnavailable,
            ConfigWriteError) as e:
        _emit_cli_error(f"{e}")
        return 2
    except FileExistsError as e:
        _emit_cli_error(f"{e}")
        return 2
    except KeyboardInterrupt:
        _emit_cli_error("init cancelled")
        return ExitCode.USER_ENDED
    if sys.stdout.isatty():
        print()
    return 0


def _run_header_line(quest_id: str, title: str, marker: str | None = None,
                     *, color: bool, intent: str | None = None) -> str:
    resolved = quest_row.resolve_display_name(title, intent)
    segments: list[str] = []
    if resolved.kind is quest_row.NameKind.TITLE:
        segments.append(resolved.text)
    elif resolved.kind is quest_row.NameKind.PROMPT:
        segments.append(quest_row._truncate(resolved.text, quest_row.TITLE_CAP))
    if marker is not None:
        segments.append(marker)
    tail = "".join(f" · {s}" for s in segments)
    head = ansi(quest_id, *pal.NODE_COLORS["quest_id"], enabled=color)
    return head + ansi(tail, "dim", enabled=color) if tail else head


def _card_header_line(quest_id: str, card: QuestCard | None, *, color: bool) -> str:
    title = card.title if card is not None else ""
    intent = (quest_row.fallback_intent(card.intent, card.seed_opening)
              if card is not None else None)
    return _run_header_line(quest_id, title, color=color, intent=intent)


def _cleared_divider(quest_id: str, leave_mode: str, *, color: bool) -> str:
    kind = {"chat": "chat", "new": "quest"}[leave_mode]
    return (ansi(f"── new {kind} ", "dim", enabled=color)
            + ansi(quest_id, *pal.NODE_COLORS["quest_id"], enabled=color)
            + ansi(" ───", "dim", enabled=color))


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
    model_sink: dict[str, str] | None = None,
    sandbox_sink: dict[str, bool] | None = None,
    hint: str = DEFAULT_HINT,
    toolbar: str | None = None,
) -> str:
    """Read one pre-session intent line, capturing `/model` into `model_sink`
    and `/sandbox` into `sandbox_sink`."""
    from imp.agents.skills import load_user_skill
    from imp.cli.abandon_confirm import abandon_question, ask_keypress_confirm, removal_description
    from imp.cli.slash import ask_line_with_slash
    from imp.remote.session.protocol import SkillRequest
    while True:
        line = await ask_line_with_slash(
            prompt, None, quest, color=color, width=width, hint=hint,
            pre_session=True, toolbar_override=toolbar, sandbox_sink=sandbox_sink)
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
        if line.strip().startswith("/sandbox "):
            if sandbox_sink is not None:
                arg = line.strip()[len("/sandbox "):].strip()
                sandbox_sink["unsandboxed"] = (arg == "off")
            continue
        if line == "/abandon":
            removed = removal_description(quest)
            if removed is not None:
                print(pal.caret(enabled=color) + line)
                if not await ask_keypress_confirm(
                        abandon_question(quest.id, quest.title, removed)):
                    print(f"Kept {quest.id}.")
                    print()
                    continue
            # No daemon pre-session. Torn down is user-ended; a failed teardown keeps its rc.
            rc = _abandon_local(quest.control_root, quest.id)
            raise SystemExit(ExitCode.USER_ENDED if rc == 0 else rc)
        return line


async def _read_opening_message(
    quest: Quest, *, leave_mode: str, banner_shown: bool, color: bool,
    width: int, editing_mode: str,
    model_sink: dict[str, str] | None = None,
    sandbox_sink: dict[str, bool] | None = None,
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
        prompt, quest, color=color, width=width, model_sink=model_sink,
        sandbox_sink=sandbox_sink, hint=OPENING_HINTS[leave_mode])
    return _normalize_opening_reply(reply)


async def _run_cleared_session(
    control: Path, base: str, *, leave_state: dict[str, str], leave_mode: str,
) -> int:
    """Open a fresh draft in place after /clear, of the caller's kind (`leave_mode`)."""
    from imp.cli.live_driver import LiveDriver
    from imp.cli.live_status import LiveStatus
    from imp.cli.session_run import (
        make_interactive_confirm,
        make_interactive_solicit,
        run_session_client,
    )
    from imp.cli.signals import InterruptCoordinator, prompt_signal_guard
    from imp.cli.terminal_title import TerminalTitle
    from imp.cli.terminal_watchdog import WatchedStream
    from imp.engine.abandon import discard_conversation
    from imp.engine.quest import Quest, live_stems
    from imp.engine.quest_id import new_quest_id

    color = should_use_color(sys.stdout)
    watched = WatchedStream(sys.stdout)
    quest = Quest.create_draft(
        control, title="", quest_id=new_quest_id(existing_stems=live_stems(control)),
        base_branch=base, origin="draft", create_mode=leave_mode)
    _session_started = False
    try:
        print(_cleared_divider(quest.id, leave_mode, color=color))
        print()
        term_title = TerminalTitle(
            watched, quest.id, quest.title, initial="idle",
            project=quest.control_root.name)
        prompt = Prompt(skills_dir=quest.workflow_dir / "skills",
                        editing_mode=_editing_mode(control), color=color, quest=quest)
        # A `/model` typed at this reopened prompt lands here; empty when none.
        cleared_model_sink: dict[str, str] = {}
        # A `/sandbox off|on` typed at this reopened prompt lands here; empty when none.
        cleared_sandbox_sink: dict[str, bool] = {}
        try:
            async with prompt_signal_guard():
                reply = await _read_intent_line(
                    prompt, quest, color=color, width=_term_width(),
                    model_sink=cleared_model_sink,
                    sandbox_sink=cleared_sandbox_sink,
                    hint=OPENING_HINTS[leave_mode])
        except (EOFError, KeyboardInterrupt):
            if term_title is not None:
                term_title.reset()
            print(_status_spine(quest.id, quest.title, "discarded", "wordmark",
                                color=color))
            print()
            return ExitCode.USER_ENDED
        _session_started = True
        cleared_model = cleared_model_sink.get("model")
        cleared_unsandboxed = bool(cleared_sandbox_sink.get("unsandboxed"))
        opening_text = _normalize_opening_reply(reply)
        # Persist the intent durably so the reopened chat's opening step seeds from
        # quest.json (mirroring the imp new path), surviving any later resume.
        quest.set_seed_opening(opening_text)
        status = LiveStatus(watched)
        driver = LiveDriver(
            stream=watched, color=color, width=_term_width,
            display_width=_display_width, status=status,
            quest_id=quest.id, title=quest.title,
            project=quest.control_root.name, preceded_by_blank=True)
        if quest.seed_opening:
            driver.seed_opening_intent(quest.seed_opening)
        coord = InterruptCoordinator()
        solicit = make_interactive_solicit(
            quest, status=status, driver=driver, coord=coord)
        confirm = make_interactive_confirm(status=status, driver=driver, coord=coord)
        return await run_session_client(
            quest, driver=driver, coord=coord, solicit=solicit, confirm=confirm,
            status=status,
            leave_mode=leave_mode,
            leave_state=leave_state, title=term_title, model=cleared_model,
            opening_unsandboxed=cleared_unsandboxed, stream=watched)
    finally:
        if not _session_started:
            discard_conversation(quest)


async def _clear_loop(
    control: Path,
    base: str,
    *,
    first: Callable[[dict[str, str]], Awaitable[int]],
    leave_mode: str,
) -> int:
    """Run first, then loop only while /clear: open a fresh conversation of the
    caller's kind, forked from `base` — the caller's, never the checkout's branch."""
    leave_state: dict[str, str] = {}
    rc = await first(leave_state)
    while leave_state.get("intent") == "clear":
        leave_state = {}
        rc = await _run_cleared_session(
            control, base, leave_state=leave_state, leave_mode=leave_mode)
    return rc


def _backlog_recorded(quest_id: str) -> str:
    return (f"Recorded backlog quest {quest_id} "
            f"(start it with `imp resume {quest_id}`).")


def _record_idea(args: argparse.Namespace, control: Path, base: str) -> int:
    from imp.cli.var_flags import VarFlagError, resolve_var_flags
    from imp.engine import backlog
    from imp.engine.errors import LoadError, LoadErrors
    from imp.engine.quest import Quest, live_stems
    from imp.engine.quest_id import new_quest_id
    title = args.idea
    if not title.strip():
        _emit_cli_error("--backlog title must be non-empty")
        return 2
    try:
        quest_vars = resolve_var_flags(
            control, workflow=getattr(args, "workflow", None),
            raw=getattr(args, "var", None))
    except (VarFlagError, LoadError, LoadErrors) as e:
        _emit_cli_error(f"{e}")
        return 2
    description = getattr(args, "opening", None) or ""
    seed = backlog.seed_opening({"title": title, "description": description})
    quest_id = new_quest_id(existing_stems=live_stems(control))
    try:
        Quest.create_draft(
            control, title=title, quest_id=quest_id, base_branch=base,
            depends_on=list(getattr(args, "depends_on", None) or []),
            workflow=getattr(args, "workflow", None),
            origin="backlog", seed_opening=seed, defer_seed=True,
            vars=quest_vars, provider=getattr(args, "provider", None))
    except (LoadError, LoadErrors) as e:
        _emit_cli_error(f"{e}")
        return 2
    _emit_human([_backlog_recorded(quest_id)])
    return 0


def apply_new(
    control: Path, *, opening: str | None, title: str | None,
    workflow: str | None, base: str | None, idea: str | None,
    depends_on: list[str] | None, parent: str | None = None,
    background: bool = False, var_flags: list[str] | None = None,
    provider: str | None = None,
) -> tuple[int, str]:
    """Non-interactive create for the mutation proxy: record a backlog quest."""
    from imp.cli.var_flags import VarFlagError, resolve_var_flags
    from imp.engine import backlog
    from imp.engine.errors import LoadError, LoadErrors
    from imp.engine.quest import Quest, live_stems
    from imp.engine.quest_id import new_quest_id
    try:
        resolved_base = _resolve_new_base(control, base)
    except BaseResolutionError as e:
        return 2, f"imp: {e}"
    try:
        quest_vars = resolve_var_flags(control, workflow=workflow, raw=var_flags)
    except (VarFlagError, LoadError, LoadErrors) as e:
        return 2, f"imp: {e}"

    deps = list(depends_on or [])
    quest_id = new_quest_id(existing_stems=live_stems(control))

    if background:
        seed = backlog.seed_opening(
            {"title": title or "", "description": opening or ""})
        try:
            Quest.create_draft(
                control, title=title or "", quest_id=quest_id,
                base_branch=resolved_base, depends_on=deps, workflow=workflow,
                origin="draft", seed_opening=seed, parent=parent,
                defer_seed=True, vars=quest_vars, provider=provider)
        except (LoadError, LoadErrors) as e:
            return 2, f"imp: {e}"
        from imp.remote.daemon import spawn
        spawn.start_quest(control, quest_id)
        return 0, (f"Started quest {quest_id} (running autonomously in the "
                   f"background; appears in the active list).")

    if idea is not None:
        if not idea.strip():
            return 2, "imp: --backlog title must be non-empty"
        seed = backlog.seed_opening({"title": idea, "description": opening or ""})
        try:
            Quest.create_draft(
                control, title=idea, quest_id=quest_id, base_branch=resolved_base,
                depends_on=deps, workflow=workflow, origin="backlog",
                seed_opening=seed, parent=parent, defer_seed=True, vars=quest_vars,
                provider=provider)
        except (LoadError, LoadErrors) as e:
            return 2, f"imp: {e}"
        return 0, _backlog_recorded(quest_id)

    try:
        Quest.create_draft(
            control, title=title or "", quest_id=quest_id,
            base_branch=resolved_base, depends_on=deps, workflow=workflow,
            origin="backlog", seed_opening=opening, parent=parent,
            defer_seed=True, vars=quest_vars, provider=provider)
    except (LoadError, LoadErrors) as e:
        return 2, f"imp: {e}"
    return 0, _backlog_recorded(quest_id)


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
    from imp.cli.live_driver import LiveDriver
    from imp.cli.live_status import LiveStatus
    from imp.cli.session_run import (
        confirm_unasked,
        make_headless_solicit,
        make_interactive_confirm,
        make_interactive_solicit,
        run_session_client,
    )
    from imp.cli.signals import InterruptCoordinator
    from imp.cli.terminal_watchdog import WatchedStream

    _print_run_header(
        quest.id, quest.title, "new", color=should_use_color(sys.stdout))
    watched = WatchedStream(sys.stdout)
    headless, auto_headless = _select_headless(args)
    autonomous = bool(getattr(args, "autonomous", False))
    if headless:
        if auto_headless:
            _emit_cli_error(
                f"no tty detected — running headless; "
                f"resume with `imp resume {quest.id}`")
        status = LiveStatus(watched)
        driver = LiveDriver(stream=watched,
                            color=should_use_color(sys.stdout),
                            width=_term_width, display_width=_display_width, status=status,
                            quest_id=quest.id, title=quest.title,
                            project=quest.control_root.name, preceded_by_blank=not auto_headless)
        coord = InterruptCoordinator()
        solicit = make_headless_solicit(getattr(args, "message", None), coord)
        return await run_session_client(
            quest, driver=driver, coord=coord, solicit=solicit, confirm=confirm_unasked,
            status=status, non_interactive=True,
            leave_mode=quest.create_mode,
            autonomous=autonomous, stream=watched)

    async def _first(leave_state: dict[str, str]) -> int:
        status = LiveStatus(watched)
        driver = LiveDriver(stream=watched,
                            color=should_use_color(sys.stdout),
                            width=_term_width, display_width=_display_width, status=status,
                            quest_id=quest.id, title=quest.title,
                            project=quest.control_root.name, preceded_by_blank=True)
        if quest.seed_opening:
            driver.seed_opening_intent(quest.seed_opening)
        coord = InterruptCoordinator()
        solicit = make_interactive_solicit(
            quest, status=status, driver=driver, coord=coord)
        confirm = make_interactive_confirm(status=status, driver=driver, coord=coord)
        return await run_session_client(
            quest, driver=driver, coord=coord, solicit=solicit, confirm=confirm,
            status=status, leave_state=leave_state,
            leave_mode=quest.create_mode,
            autonomous=autonomous, stream=watched)

    return await _clear_loop(quest.control_root, quest.base_branch, first=_first,
                             leave_mode=quest.create_mode)


def _backlog_run_only_flag(args: argparse.Namespace) -> str | None:
    if getattr(args, "non_interactive", False):
        return "--headless"
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
    from imp.engine import worktree
    from imp.providers.config import parse_default_base
    if base_flag is not None:
        if not worktree.branch_exists(control, base_flag):
            raise BaseResolutionError(
                f"--base {base_flag!r} does not name an existing branch.")
        return base_flag
    base = parse_default_base(control / "imp" / "config.yaml")
    if base is None:
        raise BaseResolutionError(
            "no `default-base` in imp/config.yaml — run `imp init` to set the "
            "branch quests integrate into.")
    if not worktree.branch_exists(control, base):
        raise BaseResolutionError(
            f"default-base {base!r} (imp/config.yaml) does not name an existing "
            "branch — update it or re-run `imp init`.")
    return base


async def _async_cmd_new(args: argparse.Namespace, *, leave_mode: str = "new") -> int:
    from imp.cli.live_driver import LiveDriver
    from imp.cli.live_status import LiveStatus
    from imp.cli.mutation_proxy import proxy_mutation_async
    from imp.cli.session_run import (
        confirm_unasked,
        make_headless_solicit,
        make_interactive_confirm,
        make_interactive_solicit,
        run_session_client,
    )
    from imp.cli.signals import prompt_signal_guard
    from imp.cli.terminal_title import TerminalTitle
    from imp.cli.terminal_watchdog import WatchedStream
    from imp.cli.var_flags import VarFlagError, resolve_var_flags
    from imp.engine import worktree
    from imp.engine.abandon import discard_conversation
    from imp.engine.errors import LoadError, LoadErrors
    from imp.engine.quest import Quest, live_stems
    from imp.engine.quest_id import new_quest_id
    from imp.providers.config import validate_workflow_name
    provider_arg = getattr(args, "provider", None)
    if provider_arg is not None:
        from imp.providers import provider_choice_error
        from imp.providers.effective import load_effective_config
        reason = provider_choice_error(
            provider_arg,
            load_effective_config(control_root=_control_root()).config)
        if reason is not None:
            _emit_cli_error(f"--provider: {reason}")
            return 2
    if os.environ.get("IMP_SESSION_SOCKET") and leave_mode == "new":
        _refuse_foreign_cwd()
        return _emit_mutation_result(await proxy_mutation_async("new", {
            "opening": getattr(args, "opening", None),
            "title": getattr(args, "title", None),
            "workflow": getattr(args, "workflow", None),
            "base": getattr(args, "base", None),
            "idea": getattr(args, "idea", None),
            "depends_on": getattr(args, "depends_on", None),
            "var_flags": getattr(args, "var", None),
            "background": getattr(args, "background", False),
            "provider": provider_arg}))
    from imp.cli.signals import InterruptCoordinator

    control = _control_root()
    worktree.check_preconditions(control)
    try:
        base = _resolve_new_base(control, getattr(args, "base", None))
    except BaseResolutionError as e:
        _emit_cli_error(f"{e}")
        return 2
    worktree.ensure_imp_ignored(control, base)

    if worktree.head_detached(control):
        _emit_human([f"imp: main checkout is at a detached HEAD — this quest "
                     f"still forks from {base}."])

    wf_arg = getattr(args, "workflow", None)
    if wf_arg is not None and not validate_workflow_name(wf_arg):
        _emit_cli_error(f"invalid workflow name {wf_arg!r}")
        return 2

    if getattr(args, "background", False):
        rc, message = apply_new(
            control, opening=getattr(args, "opening", None),
            title=getattr(args, "title", None), workflow=wf_arg,
            base=getattr(args, "base", None), idea=None,
            depends_on=list(getattr(args, "depends_on", None) or []),
            var_flags=list(getattr(args, "var", None) or []),
            background=True, provider=provider_arg)
        print(message, file=sys.stderr if rc else sys.stdout)
        return rc

    if getattr(args, "idea", None) is not None:
        if getattr(args, "title", None):
            _emit_cli_error("--backlog and --title cannot be combined (--backlog is the title)")
            return 2
        run_only = _backlog_run_only_flag(args)
        if run_only is not None:
            _emit_cli_error(
                f"--backlog files an idea instantly and never runs; {run_only} "
                f"is for headless\nruns. Drop one. Start a filed idea later with "
                f"`imp resume <id>`.")
            return 2
        if not args.idea.strip():
            _emit_cli_error("--backlog title must be non-empty")
            return 2
        return _record_idea(args, control, base)

    try:
        quest_vars = resolve_var_flags(
            control, workflow=wf_arg, raw=getattr(args, "var", None))
    except (VarFlagError, LoadError, LoadErrors) as e:
        _emit_cli_error(f"{e}")
        return 2

    try:
        seed = resolve_spec_seed(args)
    except (SeedError, ValueError) as e:
        _emit_cli_error(f"{e}")
        return 2

    # Pinned quests resolve off their own vendor's floor, not the configured order's.
    if provider_arg is None:
        block = _preflight_imp_init(control_root=control)
        if block is not None:
            _emit_cli_error(f"{block}")
            return 2

    non_interactive = bool(getattr(args, "non_interactive", False))
    autonomous = bool(getattr(args, "autonomous", False))

    interactive = _is_interactive(non_interactive=non_interactive)
    opening_text = seed.seed_text
    color = should_use_color(sys.stdout)

    quest_id = new_quest_id(existing_stems=live_stems(control))
    # No auto-title: an untitled quest has an empty title. The agent
    # titles it via rename_quest once intent is clear, or the user passes --title.
    title = seed.title if seed.title is not None else ""
    try:
        quest = Quest.create_draft(
            control, title=title, quest_id=quest_id, base_branch=base,
            workflow=wf_arg, origin="draft",
            depends_on=list(getattr(args, "depends_on", None) or []),
            seed_opening=None, create_mode=leave_mode, vars=quest_vars,
            provider=provider_arg)
    except (LoadError, LoadErrors) as e:
        _emit_cli_error(f"{e}")
        return 2

    # Build the title before the opening prompt so the escape is written at startup,
    # not after the user answers. The opening prompt means the machine is idle
    # (blocked on the user) with no daemon event yet to correct the glyph.
    show_opening_prompt = _should_prompt_opening(
        seed_text=seed.seed_text, interactive=interactive)
    watched = WatchedStream(sys.stdout)
    term_title = TerminalTitle(
        watched, quest.id, quest.title,
        initial="idle" if show_opening_prompt else "busy",
        project=quest.control_root.name)

    # Print the run header before the prompt so erase/reprint causes no layout jump.
    header_printed = False
    # A `/model` typed at the opening prompt lands here; empty when none was typed.
    opening_model_sink: dict[str, str] = {}
    # A `/sandbox off|on` typed at the opening prompt lands here; empty when none.
    opening_sandbox_sink: dict[str, bool] = {}
    if show_opening_prompt:
        try:
            async with prompt_signal_guard():
                reply = await _read_opening_message(
                    quest, leave_mode=leave_mode, banner_shown=interactive,
                    color=color, width=_term_width(),
                    editing_mode=_editing_mode(control),
                    model_sink=opening_model_sink,
                    sandbox_sink=opening_sandbox_sink)
        except (EOFError, KeyboardInterrupt):
            if term_title is not None:
                term_title.reset()
            print(_status_spine(quest.id, quest.title, "discarded", "wordmark",
                                color=color))
            print()
            discard_conversation(quest)   # drop the just-minted draft on cancel
            return ExitCode.USER_ENDED
        header_printed = True
        # An empty reply falls back to the unseeded path (the opening step
        # opens by asking); a real answer becomes the opening intent.
        opening_text = reply
    opening_model = opening_model_sink.get("model")
    opening_unsandboxed = bool(opening_sandbox_sink.get("unsandboxed"))

    # Persist the finalized intent so it survives any resume (including a restart
    # from an errored transcript). Covers both the positional and interactive
    # forms; an empty/absent reply persists as None/empty (unseeded).
    quest.set_seed_opening(opening_text)

    headless, auto_headless = _select_headless(args)
    if headless:
        if auto_headless:
            _emit_cli_error(
                f"no tty detected — running headless; "
                f"resume with `imp resume {quest.id}`")
        if not header_printed:
            _print_run_header(
                quest.id, quest.title, leave_mode, color=should_use_color(sys.stdout))
        status = LiveStatus(watched)
        driver = LiveDriver(stream=watched,
                            color=should_use_color(sys.stdout),
                            width=_term_width, display_width=_display_width, status=status,
                            quest_id=quest.id, title=quest.title,
                            project=quest.control_root.name, preceded_by_blank=True)
        coord = InterruptCoordinator()
        solicit = make_headless_solicit(None, coord)
        return await run_session_client(
            quest, driver=driver, coord=coord, solicit=solicit, confirm=confirm_unasked,
            status=status,
            title=term_title,
            leave_mode=leave_mode,
            non_interactive=True, autonomous=autonomous, model=opening_model,
            opening_unsandboxed=opening_unsandboxed, stream=watched)

    if not header_printed:
        _print_opening(quest, leave_mode, interactive=interactive, color=color)

    async def _first(leave_state: dict[str, str]) -> int:
        status = LiveStatus(watched)
        driver = LiveDriver(stream=watched,
                            color=should_use_color(sys.stdout),
                            width=_term_width, display_width=_display_width, status=status,
                            quest_id=quest.id, title=quest.title,
                            project=quest.control_root.name, preceded_by_blank=True)
        if quest.seed_opening:
            driver.seed_opening_intent(quest.seed_opening)
        coord = InterruptCoordinator()
        solicit = make_interactive_solicit(
            quest, status=status, driver=driver, coord=coord)
        confirm = make_interactive_confirm(status=status, driver=driver, coord=coord)
        return await run_session_client(
            quest, driver=driver, coord=coord, solicit=solicit, confirm=confirm,
            status=status,
            leave_mode=leave_mode,
            leave_state=leave_state, title=term_title, autonomous=autonomous,
            model=opening_model, opening_unsandboxed=opening_unsandboxed, stream=watched)

    return await _clear_loop(control, base, first=_first, leave_mode=leave_mode)


def _cmd_new(args: argparse.Namespace) -> int:
    import anyio
    return anyio.run(functools.partial(_async_cmd_new, leave_mode="new"), args)


def _cmd_chat(args: argparse.Namespace) -> int:
    import anyio
    return anyio.run(functools.partial(_async_cmd_new, leave_mode="chat"), args)


def _resume_opens_fresh(quest: Quest) -> bool:
    """Whether a resume should open a fresh seeded session rather than replay history.

    True only for an un-materialized seed draft that has never run a step; a
    quest that has ever run (and is therefore archived-resumable) always
    replays. Reads the durable per-quest fact (State.has_started) from the
    persisted state.json the resume flow already rehydrated -- never a re-scan
    of events.jsonl."""
    from imp.engine.state import State
    if quest.materialized or not quest.seed_opening:
        return False
    return not State.load(quest.state_dir).has_started


def _warn_if_blocked(quest: Quest) -> None:
    from imp.cli.store import make_store
    try:
        deps = json.loads((quest.quest_dir / "quest.json").read_bytes()).get("depends_on", [])
    except (FileNotFoundError, json.JSONDecodeError):
        return
    r = make_store(quest.control_root).backlog_readiness(deps)
    ready, unmerged = r.ready, list(r.blocked_on)
    if not ready:
        _emit_cli_error(
            f"backlog quest {quest.id} is blocked on unmerged dep(s): "
            f"{', '.join(unmerged)}; starting anyway.")


def _build_resume_driver(
    *, quest: Quest, state: State, status: LiveStatus,
    color: bool, stream: WatchedStream, preceded_by_blank: bool = False,
) -> LiveDriver:
    from imp.agents.random_status import random_status_phrase
    from imp.cli.live_driver import LiveDriver
    # startup_verb is drawn once at construction — compose_startup stores it, never re-draws.
    return LiveDriver(
        stream=stream, color=color,
        width=_term_width, display_width=_display_width, status=status,
        suppress_replay=True, preceded_by_blank=preceded_by_blank,
        startup_verb=state.step_label or random_status_phrase(),
        quest_id=quest.id, title=quest.title,
        project=quest.control_root.name)


def _resume_live_framing(
    quest_id: str, *, lock_live: bool, halt_condition: str | None,
) -> tuple[str, str | None]:
    """Marker + optional notice for a resume. A live lock means we are joining an
    already-running session, not restarting a stopped one; the running/waiting
    split matches resolve_display_status (imp list) so the wording agrees."""
    if not lock_live:
        return "resuming", None
    if halt_condition == "waiting":
        return (
            "already waiting",
            f"quest {quest_id} is already waiting for input"
            " — attaching to answer it.")
    return (
        "already running",
        f"quest {quest_id} is already running in a live session"
        " — attaching to it.")


def _print_resume_opening(
    quest: Quest, marker: str, notice: str | None, *, color: bool,
) -> None:
    from imp.cli.store import _resolve_intent
    intent = None if quest.title.strip() else quest_row.fallback_intent(
        _resolve_intent(list(quest.event_log().read())), quest.seed_opening)
    print(_run_header_line(quest.id, quest.title, marker, color=color, intent=intent))
    if notice is not None:
        print(ansi(notice, "dim", enabled=color))


async def _async_cmd_resume(args: argparse.Namespace) -> int:
    from imp.cli.inert_resume import bare_turn_end_question
    from imp.cli.live_status import LiveStatus
    from imp.cli.mutation_proxy import proxy_mutation_async
    from imp.cli.session_run import (
        confirm_unasked,
        make_headless_solicit,
        make_interactive_confirm,
        make_interactive_solicit,
        run_session_client,
    )
    from imp.cli.terminal_watchdog import WatchedStream
    from imp.engine.state import State, lock_is_live
    if os.environ.get("IMP_SESSION_SOCKET"):
        _refuse_foreign_cwd()
        return _emit_mutation_result(
            await proxy_mutation_async("resume", {"token": args.token}))
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
    from imp.engine.provider_pin import current_provider
    provider_arg = getattr(args, "provider", None)
    if provider_arg is not None:
        from imp.providers import provider_choice_error
        from imp.providers.effective import load_effective_config
        reason = provider_choice_error(
            provider_arg,
            load_effective_config(control_root=quest.control_root).config)
        if reason is not None:
            _emit_cli_error(f"--provider: {reason}")
            return 2
    if _resume_opens_fresh(quest):
        if (provider_arg is not None
                and provider_arg != current_provider(quest, None)):
            quest.set_provider(provider_arg)
        _warn_if_blocked(quest)
        return await _run_fresh_session(quest, args=args)
    state = State.load(quest.state_dir)
    # Persisted status is never `running` (it is derived from the lock at display
    # time); a crashed run is left at its resumable at-rest status (new/paused).
    if state.status not in RESUMABLE:
        _emit_cli_error(f"quest {quest.id} status is {state.status!r}; nothing to resume")
        return 2

    lock_live = lock_is_live(quest.quest_dir / "lock")
    pending_provider: str | None = None
    if (provider_arg is not None
            and provider_arg != current_provider(quest, state)):
        if lock_live:
            # A disk write behind a running engine is durable and completely
            # inert: it is re-read only at run_quest entry.
            pending_provider = provider_arg
        else:
            quest.set_provider(provider_arg)
    marker, live_notice = _resume_live_framing(
        quest.id, lock_live=lock_live,
        halt_condition=state.halt_condition)

    watched = WatchedStream(sys.stdout)
    headless, auto_headless = _select_headless(args)
    autonomous = bool(getattr(args, "autonomous", False))
    if headless:
        if auto_headless:
            _emit_cli_error(
                f"no tty detected — running headless; "
                f"resume with `imp resume {quest.id}`")
        color = should_use_color(sys.stdout)
        _print_resume_opening(quest, marker, live_notice, color=color)
        status = LiveStatus(watched)
        driver = _build_resume_driver(
            quest=quest, state=state, status=status, color=color, stream=watched)
        coord = InterruptCoordinator()
        solicit = make_headless_solicit(getattr(args, "message", None), coord)
        return await run_session_client(
            quest, driver=driver, coord=coord, solicit=solicit, confirm=confirm_unasked,
            resume=True, status=status, non_interactive=True,
            leave_mode=quest.create_mode, autonomous=autonomous,
            provider=pending_provider, stream=watched)
    if not getattr(args, "message", None):
        question = bare_turn_end_question(quest)
        if question is not None:
            return await _resume_inert(
                quest, question, marker=marker, notice=live_notice,
                autonomous=autonomous, provider=pending_provider)

    # Interactive resume: a thin session-client; the daemon owns the worktree +
    # lock and runs resume prep + the agent loop.
    color = should_use_color(sys.stdout)
    _print_resume_opening(quest, marker, live_notice, color=color)
    splash_shown = _print_splash_banner(interactive=_is_interactive(
        non_interactive=bool(getattr(args, "non_interactive", False))))

    async def _first(leave_state: dict[str, str]) -> int:
        status = LiveStatus(watched)
        driver = _build_resume_driver(
            quest=quest, state=state, status=status,
            color=should_use_color(sys.stdout), preceded_by_blank=splash_shown,
            stream=watched)
        coord = InterruptCoordinator()
        solicit = make_interactive_solicit(
            quest, status=status, driver=driver, coord=coord)
        confirm = make_interactive_confirm(status=status, driver=driver, coord=coord)
        return await run_session_client(
            quest, driver=driver, coord=coord, solicit=solicit, confirm=confirm,
            resume=True, status=status, leave_state=leave_state,
            leave_mode=quest.create_mode,
            autonomous=autonomous, provider=pending_provider, stream=watched)

    return await _clear_loop(quest.control_root, quest.base_branch, first=_first,
                             leave_mode=quest.create_mode)


def _cmd_resume(args: argparse.Namespace) -> int:
    import anyio
    return anyio.run(_async_cmd_resume, args)


def _prelaunch_position_model(quest: Quest, state: State) -> str | None:
    """Resolve the resumed step's model from state.json; None when unresolved."""
    if state.step is None or state.resolved_tiers is None:
        return None
    from imp.engine.loader import load_workflow
    from imp.engine.model_override import current_provider_model
    workflow = load_workflow(quest.workflow_dir)
    try:
        return current_provider_model(state, workflow, state.step).model
    except (StopIteration, KeyError):
        return None


async def _inert_ask_line(quest: Quest, *, toolbar: str | None = None) -> str | None:
    """Read one line at inert resume through the shared slash-aware reader
    (session-verb rejection, /help, /skills, empty-submit guard). Ctrl-D, Ctrl-C
    or a leave signal prints its trigger-qualified pause block and maps to
    None — the quest stays parked."""
    from imp.cli.signals import PromptLeave, prompt_signal_guard
    from imp.cli.store import _resolve_intent
    color = should_use_color(sys.stdout)
    width = _term_width()
    prompt = Prompt(
        skills_dir=quest.workflow_dir / "skills",
        editing_mode=_editing_mode(quest.control_root),
        color=color, quest=quest)
    events = list(quest.event_log().read())
    prompt.seed_history(_prompt_history_lines(quest.seed_opening, events))
    try:
        async with prompt_signal_guard():
            line = await _read_intent_line(
                prompt, quest, color=color, width=width, toolbar=toolbar)
    except PromptLeave as leave:
        _emit_leave_pause(quest, leave.trigger, intent=_resolve_intent(events))
        return None
    except EOFError:
        _emit_leave_pause(quest, "ctrl-d", intent=_resolve_intent(events))
        return None
    except KeyboardInterrupt:
        _emit_leave_pause(quest, "ctrl-c", intent=_resolve_intent(events))
        return None
    return line


async def _resume_inert(
    quest: Quest, question: str, *, marker: str, notice: str | None,
    autonomous: bool = False, provider: str | None = None,
) -> int:
    """AC8 inert resume: show the pending question, wait for a reply, launch live session."""
    from imp.cli.inert_resume import make_buffered_solicit, render_inert_question, run_inert_resume
    from imp.cli.live_driver import compose_prelaunch_position_row
    from imp.cli.live_status import LiveStatus
    from imp.cli.narrative import carrier_from_events
    from imp.cli.render_summary import build_summary, summary_totals
    from imp.cli.session_run import (
        make_interactive_confirm,
        make_interactive_solicit,
        run_session_client,
    )
    from imp.cli.signals import InterruptCoordinator
    from imp.cli.terminal_title import TerminalTitle
    from imp.cli.terminal_watchdog import WatchedStream
    from imp.engine.state import State

    color = should_use_color(sys.stdout)
    watched = WatchedStream(sys.stdout)
    term_title = TerminalTitle(watched, quest.id, quest.title, initial="idle",
                               project=quest.control_root.name)
    state = State.load(quest.state_dir)
    events = list(quest.event_log().read())
    _work, cost = summary_totals(build_summary(events), None)
    phase_head, _sep, plan_tail = (state.phase_label or "").partition(" · ")
    toolbar_row = compose_prelaunch_position_row(
        phase=phase_head or None, plan=plan_tail or None,
        step_label=state.step_label, model=_prelaunch_position_model(quest, state),
        quest_id=quest.id, title=quest.title, project=quest.control_root.name,
        color=color, width=_term_width(), unsandboxed=state.unsandboxed,
        carrier=carrier_from_events(events), cost=cost)
    _print_resume_opening(quest, marker, notice, color=color)
    question = render_inert_question(question, width=_term_width(), color=color)

    async def _launch(line: str) -> int:
        async def _first(leave_state: dict[str, str]) -> int:
            status = LiveStatus(watched)
            driver = _build_resume_driver(
                quest=quest, state=state, status=status,
                color=color, stream=watched)
            driver.echo_submitted_reply(line)   # visible echo, before the boot spinner
            coord = InterruptCoordinator()
            inner = make_interactive_solicit(
                quest, status=status, driver=driver, coord=coord)
            solicit = make_buffered_solicit(line, inner)
            confirm = make_interactive_confirm(status=status, driver=driver, coord=coord)
            return await run_session_client(
                quest, driver=driver, coord=coord, solicit=solicit, confirm=confirm,
                resume=True, status=status, leave_state=leave_state,
                leave_mode=quest.create_mode,
                title=term_title, autonomous=autonomous,
                provider=provider, stream=watched)

        return await _clear_loop(quest.control_root, quest.base_branch, first=_first,
                                 leave_mode=quest.create_mode)

    return await run_inert_resume(
        question=question,
        ask_line=lambda: _inert_ask_line(quest, toolbar=toolbar_row),
        launch=_launch)


def _editing_mode(control_root: Path) -> str:
    """Read editing_mode from the merged config; default to emacs if config is
    absent or invalid (run_quest reports a bad config with full context — don't
    double-fail here just to pick a prompt mode)."""
    from imp.engine.errors import LoadError, LoadErrors
    from imp.providers.effective import load_effective_config
    try:
        return load_effective_config(control_root=control_root).config.editing_mode
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


def _live_quest_ids(control: Path) -> list[str]:
    """Ids of live quest homes. They come through the store (scan_homes), not a raw
    state-dir disk scan, so an in-sandbox CLI resolves them via the session daemon:
    the fail-closed state mask hides sibling homes on disk, and the daemon reads them
    unsandboxed."""
    from imp.cli.store import make_store
    return [h.quest_id for h in make_store(control).scan_homes()]


def _with_archived_ids(control: Path, live: list[str]) -> list[str]:
    """The given live ids, then archived ids not among them, sorted."""
    from imp.cli.store import make_store
    seen = set(live)
    out = list(live)
    for cid in sorted(r.quest_id for r in make_store(control).completed_rows()):
        if cid not in seen:
            out.append(cid)
            seen.add(cid)
    return out


def _match_quest_ids(control: Path, token: str) -> list[str]:
    """Quest ids containing token, the one matching seam every command shares.

    Live homes match first and any live hit returns without touching the archive, so
    a live quest resolves whatever state the archive is in and archived ids sharing
    its stem are not candidates. Zero live hits fall through to the merged
    live+archived space — reusing the live list already scanned — which still raises
    honestly when the token genuinely needs the archive."""
    live = _live_quest_ids(control)
    hits = [cid for cid in live if token in cid]
    if hits:
        return hits
    return [cid for cid in _with_archived_ids(control, live) if token in cid]


def _title_for_id(control: Path, quest_id: str) -> str | None:
    """Read a quest's current title (worktree home first, archive second)."""
    from imp.cli.store import _quest_home
    from imp.engine.archive import read as archive_read
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
        _emit_cli_error(f"no quest matches {token!r}")
        raise SystemExit(2)
    matches = _match_quest_ids(control, token)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        _exit_ambiguous(token, matches, control)
    _emit_cli_error(f"no quest matches {token!r}")
    raise SystemExit(2)


def _exit_ambiguous(token: str, candidates: list[str], control: Path) -> NoReturn:
    from imp.cli.store import make_store
    _emit_cli_error(f"{token!r} is ambiguous; candidates:")
    color = should_use_color(sys.stderr)
    now = datetime.now(UTC)
    store = make_store(control)
    cards = [(cid, store.quest_card(cid)) for cid in sorted(candidates)]
    intents = {c.quest_id: quest_row.fallback_intent(c.intent, c.seed_opening)
               for _, c in cards if c is not None}
    status_w, title_w = quest_row.overview_widths([
        (c.status, quest_row.compose_status_label(c.step, c.step_label, c.phase_label),
         quest_row.resolve_display_name(c.title, intents[c.quest_id]).text)
        for _, c in cards if c is not None])
    print(quest_row.header_line(color=color, title_width=title_w, status_width=status_w),
          file=sys.stderr)
    for cid, card in cards:
        if card is None:
            print(f"  {cid}", file=sys.stderr)
            continue
        print(quest_row.format_quest_line(
            quest_id=card.quest_id, title=card.title, status=card.status,
            step=card.step, step_label=card.step_label,
            phase_label=card.phase_label, last_iso=card.last,
            now=now, color=color, title_width=title_w, status_width=status_w,
            intent=intents[card.quest_id]),
            file=sys.stderr)
    raise SystemExit(2)


def _prompt_history_lines(
    seed_opening: str | None, events: Iterable[dict[str, Any]],
) -> list[str]:
    from imp.cli.store import _user_message_text
    lines: list[str] = []
    if seed_opening and seed_opening.strip():
        lines.append(seed_opening)
    for ev in events:
        if ev.get("type") != "user_message":
            continue
        payload = ev.get("payload") or {}
        if payload.get("subtype") == "intent":
            continue          # the opening is already represented by seed_opening
        text = _user_message_text(payload)
        if text.strip():
            lines.append(text)
    return lines


def _first_user_message(events: list[dict[str, Any]]) -> str | None:
    from imp.cli.store import _user_message_text
    for ev in events:
        if ev.get("type") == "user_message":
            text = _user_message_text(ev.get("payload") or {}).strip()
            if text:
                return text
    return None


def _unstarted_backlog_meta(control: Path, quest_id: str) -> dict[str, Any] | None:
    """quest.json of a LIVE un-materialized, non-draft-origin, never-conversed,
    unlocked Backlog home, read through the store so a
    sandboxed client gets the daemon's answer rather than a masked home. Sourced
    from the live-home scan only, never the archive: an abandoned home leaves an
    archived materialized:false quest.json that would otherwise render as a
    queued `status: backlog` brief instead of a History item. None for an id
    absent from the scan (archived-only) and for any home the shared backlog
    predicate rejects (materialized, draft-origin, already-conversed, or
    lock-live)."""
    from imp.cli.store import _is_backlog_home, make_store
    digest = next(
        (h for h in make_store(control).scan_homes() if h.quest_id == quest_id), None)
    if digest is None:
        return None
    if not _is_backlog_home(digest.meta, conversed=digest.has_user_message,
                            lock_live=digest.lock_live):
        return None
    return digest.meta


def _card_field(label: str, value: str, width: int, *, color: bool) -> str:
    """A card metadata row: the `label:` glyph dimmed (SGR 2 when color is on),
    padded to `width` columns, then the plain value at its aligned column."""
    return pal.ansi(label, "dim", enabled=color) + " " * (width - len(label)) + value


def _card_title_row(title: str, intent: str | None, width: int, *, color: bool) -> str:
    """The `title:` row: a real title plain, else the dim fallback clipped to one line."""
    resolved = quest_row.resolve_display_name(title, intent)
    if resolved.kind is quest_row.NameKind.TITLE:
        return _card_field("title:", resolved.text, width, color=color)
    value = pal.ansi(resolved.text, "dim", enabled=color)
    return pal.clip_display(_card_field("title:", value, width, color=color),
                            _display_width())


def _backlog_card(meta: dict[str, Any], control: Path, *, now: datetime,
                  color: bool) -> list[str]:
    from imp.cli.store import make_store
    r = make_store(control).backlog_readiness(meta.get("depends_on", []))
    ready, unmerged = r.ready, list(r.blocked_on)
    readiness = "ready" if ready else f"blocked on {', '.join(unmerged)}"
    created = meta.get("created_at")
    lines = [
        _card_field("id:", meta["id"], 12, color=color),
        _card_title_row(meta.get("title") or "", meta.get("seed_opening"), 12,
                        color=color),
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
    quest_vars: dict[str, object] = meta.get("vars", {})
    lines.extend(_vars_row(quest_vars, 12, color=color))
    body = meta.get("seed_opening") or ""
    title = meta.get("title") or ""
    if title and (body == title or body.startswith(title + "\n")):
        body = body[len(title):].lstrip("\n")
    if body:
        lines.append("")
        lines.append(pal.ansi("intent:", "dim", enabled=color))
        lines.append(body)
    lines.extend(_vars_blocks(quest_vars, color=color))
    return lines


def _var_value(value: object) -> str:
    """Renders the value the way `--var` accepts it back."""
    return str(value).lower() if isinstance(value, bool) else str(value)


_LONG_VAR_CHARS = 80


def _is_long_var(text: str) -> bool:
    return "\n" in text or len(text) > _LONG_VAR_CHARS


def _vars_row(values: dict[str, object], width: int, *, color: bool) -> list[str]:
    """The `vars:` rows for short vars: entries wrapped whole at `, `, never clipped."""
    texts = {name: _var_value(values[name]) for name in sorted(values)}
    entries = [f"{name}={text}" for name, text in texts.items() if not _is_long_var(text)]
    avail = _term_width() - width
    rows: list[str] = []
    for i, entry in enumerate(entries):
        joined = f"{rows[-1]}, {entry}" if rows else entry
        trailing_comma = 1 if i < len(entries) - 1 else 0
        if rows and pal.display_width(joined) + trailing_comma <= avail:
            rows[-1] = joined
        else:
            rows.append(entry)
    if not rows:
        return []
    rows = [row + "," for row in rows[:-1]] + rows[-1:]
    return [_card_field("vars:", rows[0], width, color=color),
            *(" " * width + row for row in rows[1:])]


def _vars_blocks(values: dict[str, object], *, color: bool) -> list[str]:
    """Each long var as its own dim `vars.<name>:` section, value verbatim."""
    lines: list[str] = []
    for name in sorted(values):
        text = _var_value(values[name])
        if _is_long_var(text):
            lines += ["", pal.ansi(f"vars.{name}:", "dim", enabled=color),
                      *text.rstrip("\n").split("\n")]
    return lines


# Short card phrasing for the halt reasons a bare token does not explain; the long
# `imp log` copy is render_text._PAUSE_REASON_COPY. Unlisted tokens fall through
# _card_reason_phrase's chain.
_CARD_PAUSE_PHRASE: dict[str, str] = {
    "detached": "the terminal went away",
    "sigterm": "interrupted",
    "interrupt": "interrupted",
    "reauth_needed": "provider login expired",
    "sandbox_spawn_failed": "the sandbox could not start",
    "max_loops": "a step hit its loop limit",
    "context_window": "a step used up its context handoffs",
    "quota": "quota exhausted",
    "script_blocked": "a script step was blocked",
    "clean_tree": "a step left uncommitted changes",
    "unknown_step": "the current step is missing from the workflow",
    "model-override": "model switch",
    "compact": "/compact",
    "awaiting_turn": "waiting for its turn to merge",
}


def _card_reason_phrase(token: str) -> str:
    return (_CARD_PAUSE_PHRASE.get(token) or _PAUSE_QUALIFIER.get(token)
            or _ERROR_LABELS.get(token, token))


def _card_pause_token(status: str, pause_reason: str | None) -> str | None:
    """The halt reason `imp show` surfaces, card and --json alike: only the two halt
    statuses whose reason is not already the status word (`merge_blocked` restates
    itself; `new`/`running`/`waiting` are not halted)."""
    return pause_reason if status in ("paused", "error") else None


class _CardLedger(NamedTuple):
    """What the card folds in from the run ledger: the `run:` value and the table
    minus its run header line."""

    run: str
    body: list[str]


def _run_field(summary: list[StepRow], header: SummaryHeader,
               quota: QuotaColumn | QuotaFallback | None) -> str:
    """The `run:` value: the ledger's own total row on one line. ` weekly*` marks a
    resolved gauge cell, which is a share of a window rather than money."""
    from imp.cli.quota_column import NO_FIGURE, QuotaColumn
    from imp.cli.render_summary import (
        _dur_text,
        _fmt_tokens,
        summary_totals,
        total_cell,
    )
    work, total = summary_totals(summary, header.total_seconds, header.exclusions)
    charge = total_cell(summary, quota)
    if isinstance(quota, QuotaColumn) and charge != NO_FIGURE:
        charge = f"{charge} weekly*"
    return (f"{_dur_text(work)} · {_fmt_tokens(total.input_total)} in"
            f" · {_fmt_tokens(total.output)} out · {charge}")


def _summary_header(quest_id: str, card: QuestCard | None,
                    events: list[dict[str, Any]]) -> SummaryHeader:
    from imp.cli.render_summary import SummaryHeader, exclusion_spans, summary_total
    return SummaryHeader(
        quest_id=quest_id,
        title=card.title if card is not None else "",
        status=card.status if card is not None else "?",
        total_seconds=summary_total(events),
        intent=(quest_row.fallback_intent(card.intent, card.seed_opening)
                if card is not None else None),
        exclusions=exclusion_spans(events))


def _card_ledger(store: Store, card: QuestCard, *, color: bool,
                 width: int) -> _CardLedger | int | None:
    """None when the quest has recorded no step rows yet — the card is then what it
    was before these fields existed. 2 propagates `_attribute_quota`'s unreachable
    store, which must print no card at all."""
    from imp.cli.render_summary import (
        build_summary,
        render_summary,
    )
    events = store.get_events(card.quest_id) or []
    rows = build_summary(events)
    if not rows:
        return None
    header = _summary_header(card.quest_id, card, events)
    view = _attribute_quota(store, card.quest_id, events, rows)
    if isinstance(view, int):
        return view
    return _CardLedger(
        run=_run_field(rows, header, view),
        body=render_summary(rows, header=header, color=color, width=width,
                            quota=view, header_line=False, hide_idle=True))


def _quest_card(card: QuestCard, *, now: datetime, color: bool,
                ledger: _CardLedger | None) -> list[str]:
    from imp.cli.store import _INFLIGHT
    lines = [
        _card_field("id:", card.quest_id, 15, color=color),
        _card_title_row(card.title,
                        quest_row.fallback_intent(card.intent, card.seed_opening),
                        15, color=color),
        _card_field(
            "status:",
            quest_row.display_word(card.status),
            15, color=color),
    ]
    reason = _card_pause_token(card.status, card.pause_reason)
    if reason:
        lines.append(_card_field(
            "reason:", _card_reason_phrase(reason), 15, color=color))
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
    if ledger is not None:
        lines.append(_card_field("run:", ledger.run, 15, color=color))
        lines.append(_card_field("workflow:", card.workflow, 15, color=color))
    if card.base_branch:
        lines.append(_card_field("base_branch:", card.base_branch, 15, color=color))
    if card.parent:
        lines.append(_card_field(
            "parent:", f"{card.parent['id']} · {card.parent['title']}",
            15, color=color))
    if card.depends_on:
        deps = ", ".join(
            f"{d['id']} · {d['title']} ({'merged' if d['merged'] else 'unmerged'})"
            for d in card.depends_on)
        lines.append(_card_field("depends_on:", deps, 15, color=color))
    lines.extend(_vars_row(card.seeded_vars, 15, color=color))
    if card.merge_commit:
        lines.append(_card_field("merge_commit:", card.merge_commit, 15, color=color))
    if card.intent:
        lines.append("")
        lines.append(pal.ansi("intent:", "dim", enabled=color))
        lines.append(card.intent)
    if card.summary:
        lines.append("")
        lines.append(pal.ansi("summary:", "dim", enabled=color))
        lines.extend(
            card.summary.decode("utf-8", errors="replace").rstrip("\n").splitlines())
    lines.extend(_vars_blocks(card.seeded_vars, color=color))
    if card.files:
        lines.append("")
        lines.append(pal.ansi("files:", "dim", enabled=color))
        lines.extend(_display_name(f) for f in card.files)
    if ledger is not None:
        lines.append("")
        lines.append(pal.ansi("ledger:", "dim", enabled=color))
        lines.extend(ledger.body)
    return lines


def _detail_card(control: Path, quest_id: str, *, now: datetime,
                 color: bool) -> list[str] | int:
    from imp.cli.store import make_store
    meta = _unstarted_backlog_meta(control, quest_id)
    if meta is not None:
        return _backlog_card(meta, control, now=now, color=color)
    store = make_store(control)
    card = store.quest_card(quest_id)
    if card is None:
        return []
    ledger = _card_ledger(store, card, color=color, width=_term_width())
    if isinstance(ledger, int):
        return ledger
    return _quest_card(card, now=now, color=color, ledger=ledger)


def _quest_json(control: Path, quest_id: str) -> dict[str, Any] | None:
    from imp.cli.store import _INFLIGHT, make_store
    card = make_store(control).quest_card(quest_id)
    if card is None:
        return None
    data: dict[str, Any] = {
        "id": card.quest_id,
        "title": card.title.strip() or None,
        "status": card.status,
        "pause_reason": _card_pause_token(card.status, card.pause_reason),
        "created": card.created or None,
        "last_activity": card.last or None,
        "base_branch": card.base_branch,
        "merge_commit": card.merge_commit,
        "parent": card.parent,
        "depends_on": card.depends_on,
        "intent": card.intent,
        "vars": dict(card.vars),
        "summary": None,
        "files": list(card.files),
    }
    if card.status in _INFLIGHT and card.step not in ("", "-"):
        data["step"] = {"phase": card.phase_label or None,
                        "name": card.step_label or card.step}
    if card.summary:
        data["summary"] = card.summary.decode("utf-8", errors="replace").strip() or None
    return data


def _backlog_json(meta: dict[str, Any], control: Path) -> dict[str, Any]:
    from imp.cli.store import make_store
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
        "vars": dict(meta.get("vars") or {}),
        "body": body or None,
    }


def _cmd_list(args: argparse.Namespace) -> int:
    from imp.engine import worktree
    try:
        control = _control_root()
    except worktree.PreconditionError:
        if args.json:
            _page_or_print(json.dumps([], indent=2).splitlines(),
                           no_pager=args.no_pager)
            return 0
        _emit_human([_NO_QUESTS_MESSAGE])
        return 0
    now = datetime.now(UTC)
    color = should_use_color(sys.stdout)
    dispositions = _selected_list_dispositions(args)
    if not dispositions and args.since is None:
        if args.json:
            _page_or_print(
                json.dumps(_board_json(
                    control, dispositions=_board_dispositions(show_all=args.all),
                    cutoff=None), indent=2).splitlines(),
                no_pager=args.no_pager)
            return 0
        lines = _overview_lines(control, show_all=args.all, now=now, color=color)
        _page_or_print(lines, no_pager=args.no_pager, trailing_blank=True)
        return 0
    try:
        cutoff = _since_cutoff(args.since, now) if args.since is not None else None
    except ValueError:
        _emit_cli_error(_SINCE_ERROR)
        raise SystemExit(2) from None
    selected = dispositions or set(_DEFAULT_LIST_DISPOSITIONS)
    if args.json:
        _page_or_print(
            json.dumps(_board_json(control, dispositions=selected, cutoff=cutoff),
                       indent=2).splitlines(),
            no_pager=args.no_pager)
        return 0
    lines = _filtered_overview_lines(
        control, dispositions=selected, cutoff=cutoff, now=now, color=color)
    _page_or_print(lines, no_pager=args.no_pager, trailing_blank=True)
    return 0


def _saved_file_matches(files: list[str], name: str) -> list[str]:
    key = name.removesuffix("/")
    if key in files:
        return [key]
    return [f for f in files if f.startswith(f"{key}/")]


def _display_name(name: str) -> str:
    return name.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


def _cmd_show(args: argparse.Namespace) -> int:
    from imp.cli.store import make_store
    token = args.token
    if token is None:
        _emit_missing_quest_id("show")
        return 2
    control = _control_root()
    try:
        quest_id = _resolve_id(control, token)
    except SystemExit:
        return 2
    name = args.name
    meta = _unstarted_backlog_meta(control, quest_id)
    if meta is not None:
        if name is None:
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
        _emit_cli_error(f"{quest_id} is in the backlog (not started); no saved files yet")
        return 2
    if name is None:
        if args.json:
            card = _quest_json(control, quest_id)
            if card is None:
                _emit_cli_error(f"no such quest {quest_id!r}")
                return 2
            _page_or_print(json.dumps(card, indent=2).splitlines(),
                           no_pager=args.no_pager)
            return 0
        lines = _detail_card(control, quest_id, now=datetime.now(UTC),
                             color=should_use_color(sys.stdout))
        if isinstance(lines, int):
            return lines
        _page_or_print(lines, no_pager=args.no_pager, trailing_blank=True)
        return 0
    store = make_store(control)
    matches = _saved_file_matches(store.saved_files(quest_id), name)
    if len(matches) > 1:
        shown = [_display_name(m) for m in matches]
        _page_or_print(shown, no_pager=args.no_pager, trailing_blank=True)
        return 0
    data = store.read_saved_file(quest_id, matches[0]) if matches else None
    if data is None:
        _emit_cli_error(f"{quest_id} has no saved file '{name}'\n"
                        f"  list its saved files with: imp show {quest_id}")
        return 2
    _page_or_print(data.decode("utf-8", errors="replace").splitlines(),
                   no_pager=args.no_pager, trailing_blank=True)
    return 0


def _resolve_row_intent(control: Path, r: _Row) -> str | None:
    """Fallback display name for a title-less row: its opening prompt, else None."""
    from imp.cli.store import _load_events_for_id, _resolve_intent
    if r[4].strip():
        return None
    return _resolve_intent(_load_events_for_id(control, r[0]) or [])


def _row_display_intent(control: Path, r: _Row) -> str | None:
    """A title-less row's human name source: its opening prompt, else its seed_opening."""
    from imp.cli.store import _display_intent_for_id
    if r[4].strip():
        return None
    return _display_intent_for_id(control, r[0])


_NO_QUESTS_MESSAGE = "No quests yet — start one with imp new"
_NO_ACTIVE_QUESTS_MESSAGE = "No active quests — see imp list -a for history"


def _emit_missing_quest_id(command: str) -> None:
    """Bare `imp <command>`: name the missing id; outside a repo counts as no quests."""
    from imp.engine import worktree
    try:
        control = _control_root()
    except worktree.PreconditionError:
        any_quest = False
    else:
        rows = _overview_rows(control, dispositions=_board_dispositions(show_all=True),
                              cutoff=None)
        any_quest = bool(rows.inflight or rows.backlog or rows.history)
    if any_quest:
        _emit_cli_error(f"{command} needs a quest id — see imp list")
    else:
        _emit_cli_error(f"{command} needs a quest id\n  {_NO_QUESTS_MESSAGE}")


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
    """Return the first output-mode flag `--follow` conflicts with, or None.

    The content flags (--full/--show/--hide) compose — only whole other output
    modes clash."""
    for name, present in (
        ("--summary", args.summary),
        ("--html", args.html is not None),
        ("--md", args.md is not None),
    ):
        if present:
            return name
    return None


def _follow_log(events: list[dict[str, Any]], *, cats: frozenset[str],
                project: str = "") -> int:
    """Replay a quest's narrative through the live run view's renderer."""
    from imp.cli.live_driver import LiveDriver
    from imp.cli.live_status import LiveStatus
    status = LiveStatus(sys.stdout)
    driver = LiveDriver(
        stream=sys.stdout, color=should_use_color(sys.stdout),
        width=_term_width, display_width=_display_width, status=status,
        project=project, cats=cats)
    for event in events:
        driver.on_event(event)
    driver.finish(exit_block=False)
    return 0


@dataclass
class _StuckLine:
    """The event line currently failing to parse: its bytes, the decode error
    that rejected them, and how many read_new polls have seen it."""
    bad: bytes
    reason: str
    polls: int


class _EventTail:
    """Offset-tracked, partial-line-tolerant reader for a live events.jsonl.

    Reads only complete, newline-terminated lines from a kept-open descriptor.
    A complete line that fails to parse (a transient btrfs+zstd read fault, or a
    torn append) halts consumption at that byte offset and is retried on the
    next read — it never raises. Each such fault is recorded once to the
    diagnostics log (imp.engine.readfault), and once more if it recovers.
    `emitted` counts every event handed out."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fp: Any = None
        self._offset = 0
        self._stuck: _StuckLine | None = None
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
        fault: tuple[bytes, str] | None = None
        for chunk in data.split(b"\n")[:-1]:
            try:
                events.append(json.loads(chunk.decode("utf-8")))
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                fault = (chunk, str(e))
                break
            consumed += len(chunk) + 1
        if self._stuck is not None or fault is not None:
            self._note_fault(data, consumed, fault)
        self._offset += consumed
        self.emitted += len(events)
        return events

    def _note_fault(self, data: bytes, consumed: int,
                    fault: tuple[bytes, str] | None) -> None:
        """Record this poll's state changes for the line at the current offset:
        one line when it first fails to parse, one when it finally reads
        cleanly. While a line is stuck the offset cannot advance, so the slot
        itself dedupes — a stuck poll loop records nothing. Imports are lazy:
        this path is rare and CLI startup must not pay for it."""
        from imp.engine import pagecache, readfault
        stuck = self._stuck
        if stuck is not None:
            stuck.polls += 1
            if consumed:                      # the stuck line parsed this poll
                readfault.record(
                    "event_tail", self._path,
                    attempt=stuck.polls, reason=stuck.reason, bad=stuck.bad,
                    good=data.split(b"\n")[0])
                self._stuck = None
        if fault is not None and self._stuck is None:
            bad, reason = fault
            self._stuck = _StuckLine(bad=bad, reason=reason, polls=1)
            readfault.record("event_tail", self._path,
                             attempt=None, reason=reason, bad=bad, good=None)
            pagecache.drop_page_cache(self._path)


_FOLLOW_TERMINAL = frozenset({"completed", "abandoned", "archived"})


def _follow_paused_status(quest_id: str) -> Callable[[int], list[str]]:
    """A static one-row footer composer shown while the run is paused."""
    line = f"paused — resume with imp resume {quest_id}"
    return lambda frame: [line]


def _drain_from_archive(driver: LiveDriver, tail: _EventTail,
                        control: Path, quest_id: str) -> int:
    """The home vanished mid-follow: feed the archived events beyond the count
    already emitted (none if the id is not archived), finish, and exit 0."""
    from imp.cli.store import _parse_jsonl
    from imp.engine.archive import read as archive_read
    raw = archive_read.read_events(control, quest_id)
    if raw is not None:
        for event in _parse_jsonl(raw)[tail.emitted:]:
            driver.on_event(event)
    driver.finish(exit_block=False)
    return 0


def _follow_step(driver: LiveDriver, tail: _EventTail, status: LiveStatus,
                 control: Path, quest_id: str, home: Path) -> int | None:
    """One poll iteration. Feeds newly-appended events through the driver and
    drives the footer from the lock's liveness. Returns an exit code when the
    follow should stop (terminal status, or the home vanished into the archive),
    else None to keep polling."""
    from imp.engine.state import State, lock_is_live
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
        driver.finish(exit_block=False)
        return 0
    if lock_is_live(home / "lock"):
        status.update(driver.compose_status)
    else:
        status.update(_follow_paused_status(quest_id))
    return None


_FOLLOW_POLL_INTERVAL = 0.3


def _follow_live(control: Path, quest_id: str, home: Path, *,
                 cats: frozenset[str]) -> int:
    """Replay a live quest's narrative through LiveDriver, then tail
    events.jsonl until a terminal status or the home is archived away. Ctrl-C
    clears the footer and exits 130."""
    import anyio

    from imp.cli.live_driver import LiveDriver
    from imp.cli.live_status import LiveStatus
    status = LiveStatus(sys.stdout)
    driver = LiveDriver(
        stream=sys.stdout, color=should_use_color(sys.stdout),
        width=_term_width, display_width=_display_width, status=status,
        project=control.name, cats=cats)
    tail = _EventTail(home / "events.jsonl")
    try:
        return anyio.run(
            _follow_poll_loop, driver, tail, status, control, quest_id, home)
    except KeyboardInterrupt:
        status.reset()
        return ExitCode.USER_ENDED


async def _follow_poll_loop(
    driver: LiveDriver, tail: _EventTail, status: LiveStatus,
    control: Path, quest_id: str, home: Path,
) -> int:
    """Advance the spinner (LiveStatus.tick) concurrently with a poll loop that
    feeds new events and checks exit conditions every _FOLLOW_POLL_INTERVAL."""
    import anyio
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


def _reject_unknown_step(
    steps: set[str], indexed: list[tuple[int, dict[str, Any]]], quest_id: str,
) -> bool:
    """Report one requested step this quest never ran, listing the ids it did."""
    from imp.engine import events_index as ei
    known = [s.name for s in ei.step_windows(indexed)]
    missing = sorted(steps - set(known))
    if not missing:
        return False
    listing = f"; steps: {', '.join(known)}" if known else ""
    _emit_cli_error(f"no step {missing[0]!r} in {quest_id}{listing}")
    return True


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
        _emit_cli_error(f"{exc}")
        return 2
    if steps is not None and _reject_unknown_step(steps, indexed, quest_id):
        return 2
    selected = ei.select_event_lines(
        indexed, agents=agents, ranges=intervals or None,
        step_names=steps, limit=args.limit, handle_map=handle_map)
    return [ev for _, ev in selected]


def _log_tree(events: list[dict[str, Any]],
              unfiltered: list[dict[str, Any]]) -> NarrativeTree:
    """The narrative of `events`, each subagent showing the state it reached in the whole log."""
    from imp.cli import narrative
    tree = narrative.build_tree(events)
    if events is not unfiltered:
        full = narrative.build_tree(unfiltered)
        narrative.overlay_subagent_state(tree, full)
        narrative.overlay_leave_marker(tree, full)
    return tree


def _log_summary(events: list[dict[str, Any]],
                 unfiltered: list[dict[str, Any]]) -> list[StepRow]:
    """The summary rows of `events`, each span ending where it did in the whole log."""
    from imp.cli.render_summary import build_summary, overlay_spans
    rows = build_summary(events)
    if events is not unfiltered:
        overlay_spans(rows, build_summary(unfiltered))
    return rows


def _cmd_log(args: argparse.Namespace) -> int:
    from imp.cli.render_text import LOG_CATEGORIES, render_tree
    from imp.cli.store import _quest_home, make_store

    show_names = args.show or []
    hide_names = args.hide or []
    unknown = sorted((set(show_names) | set(hide_names)) - set(LOG_CATEGORIES))
    if unknown:
        _emit_cli_error(f"unknown category {unknown[0]!r} "
                        f"(choose from: {', '.join(LOG_CATEGORIES)})")
        return 2
    if args.summary and (args.show is not None or args.hide is not None):
        _emit_cli_error("--show/--hide not allowed with -s/--summary")
        return 2
    show = _log_show_set(args.full, show_names, hide_names)

    if args.follow:
        conflict = _follow_conflict(args)
        if conflict is not None:
            _emit_cli_error(f"-f/--follow not allowed with {conflict}")
            return 2
        args.no_pager = True
    if args.token is None:
        _emit_missing_quest_id("log")
        return 2
    control = _control_root()
    quest_id: str
    try:
        quest_id = _resolve_id(control, args.token)
    except SystemExit:
        return 2
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
        _emit_cli_error(f"no such quest {quest_id!r}")
        return 2

    if args.follow:
        if os.environ.get("IMP_SESSION_SOCKET"):
            _emit_cli_error("live follow is unavailable inside the sandbox; "
                            "showing a one-shot snapshot.")
            return _follow_log(events, cats=show, project=control.name)
        home = _quest_home(control, quest_id)
        if home.exists():
            return _follow_live(control, quest_id, home, cats=show)
        return _follow_log(events, cats=show, project=control.name)

    unfiltered = events
    selected = _select_log_events(args, quest_id, events)
    if isinstance(selected, int):
        return selected
    events = selected

    if args.html is not None or args.md is not None:
        return _write_log_export(args, store, quest_id, events, unfiltered,
                                 show=show)

    width = _term_width()
    color = should_use_color(sys.stdout)

    if args.summary:
        from imp.cli.render_summary import render_summary
        card = store.quest_card(quest_id)
        header = _summary_header(quest_id, card, events)
        rows = _log_summary(events, unfiltered)
        view = _attribute_quota(store, quest_id, unfiltered, rows)
        if isinstance(view, int):
            return view
        lines = render_summary(rows, header=header,
                               color=color, width=width, quota=view)
        _page_or_print(lines, no_pager=args.no_pager, trailing_blank=True)
        return 0

    tree = _log_tree(events, unfiltered)
    run_header = _card_header_line(quest_id, store.quest_card(quest_id), color=color)
    lines = [run_header, "", *render_tree(tree, show=show, color=color, width=width,
                                          display_width=_display_width())]
    _page_or_print(lines, no_pager=args.no_pager, trailing_blank=True)
    return 0


def _attribute_quota(
    store: Store, quest_id: str, events: list[dict[str, Any]],
    rows: list[StepRow],
) -> QuotaColumn | QuotaFallback | int | None:
    """This run's quota column, overlaid onto `rows` — which a windowing flag may
    have narrowed, so the charge is computed over the unfiltered log and a
    step's cell reads the same either way. None is a metered run, which renders
    today's output untouched and never reaches the store; 2 is an unreachable
    store, since an under-counted denominator must never pass as a figure."""
    from imp.cli.quota_column import quota_view
    from imp.cli.quota_share import charges_by_index
    from imp.cli.render_summary import (
        build_summary,
        charges_by_row,
        overlay_charges,
        rows_charge,
    )
    if not any(r.totals.notional for r in build_summary(events)):
        return None   # metered by construction: today's output, untouched
    try:
        weeks = store.quota_weeks(quest_id)
    except (OSError, RuntimeError, ValueError) as exc:
        _emit_cli_error("cannot reach the quest store for quota attribution "
                        f"(quota_weeks): {exc}")
        return 2
    full = build_summary(events, charges_by_index(events, weeks))
    overlay_charges(rows, charges_by_row(full))
    return quota_view(rows_charge(full))


def _write_log_export(args: argparse.Namespace, store: Store,
                      quest_id: str, events: list[dict[str, Any]],
                      unfiltered: list[dict[str, Any]], *,
                      show: frozenset[str]) -> int:
    card = store.quest_card(quest_id)
    if args.summary:
        from imp.cli.render_markdown import render_summary_markdown
        header = _summary_header(quest_id, card, events)
        rows = _log_summary(events, unfiltered)
        view = _attribute_quota(store, quest_id, unfiltered, rows)
        if isinstance(view, int):
            return view
        document = render_summary_markdown(rows, header=header, quota=view)
    else:
        from imp.cli.render_markdown import render_markdown
        document = render_markdown(_log_tree(events, unfiltered), full=args.full,
                                   show=show)
    if args.html is not None:
        from imp.cli.render_html import render_html
        page_title = _card_header_line(quest_id, card, color=False)
        if args.summary:
            page_title += " · summary"
        document = render_html(document, title=page_title)
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


def _journal_status(control: Path, quest_id: str) -> str | None:
    from imp.engine.archive import read as archive_read
    return archive_read.status_of(control, quest_id)


def _resume_gone(control: Path, quest_id: str) -> int:
    from imp.engine.archive import read as archive_read
    status = _journal_status(control, quest_id)
    title = _title_for_id(control, quest_id) or quest_id
    if status == "abandoned":
        print(f"{title} was abandoned; nothing to resume.")
        return 0
    if status is not None:
        word = "merged" if archive_read.merge_commit_of(control, quest_id) else "completed"
        print(f"{title} is already {word}; nothing to resume.")
        return 0
    _emit_cli_error(f"no quest matches {quest_id!r}")
    return 2


def _rehydrate_resumable(control: Path, quest_id: str) -> Quest | None:
    from imp.engine.archive import read as archive_read
    from imp.engine.archive import record, records_root, require_current
    from imp.engine.errors import LoadError, LoadErrors
    from imp.engine.quest import Quest
    require_current(control)
    rec = record.read_record(records_root(control), quest_id)
    if rec is None or not rec.get("resumable"):
        return None
    try:
        archive_read.rehydrate_quest(control, quest_id)
        return Quest.load(control, quest_id)
    except (FileExistsError, LoadError, LoadErrors):
        return None


def _load_or_rehydrate(control: Path, quest_id: str) -> Quest | int:
    from imp.engine.errors import LayoutMismatch, LoadError, LoadErrors
    from imp.engine.quest import Quest
    try:
        return Quest.load(control, quest_id)
    except LayoutMismatch as e:
        _emit_cli_error(f"{e}")
        return 2
    except (LoadError, LoadErrors):
        quest = _rehydrate_resumable(control, quest_id)
        if quest is None:
            return _resume_gone(control, quest_id)
        return quest


RESUMABLE = ("new", "paused", "error", "merge_blocked", "archived")


def _completed_rows(control: Path, live_ids: set[str] | None = None) -> list[_Row]:
    """Completed/abandoned quests NOT currently surfaced in-flight, from a
    SINGLE indexed archive query (spec AC 7). `live_ids` lets the overview reuse
    the in-flight id set it already computed instead of re-deriving it."""
    from imp.cli.store import _INFLIGHT, _status_rows, make_store
    live = {r[0] for r in _status_rows(control)} if live_ids is None else live_ids
    rows: list[_Row] = []
    for r in make_store(control).completed_rows():
        if r.quest_id in live or r.status in _INFLIGHT or r.status == "archived":
            continue  # archived rows come only from _archived_rows (never double-listed)
        rows.append((r.quest_id, r.status, r.step, r.last, r.title, r.created, None, None, None))
    return rows


def _archived_rows(control: Path, live_ids: set[str] | None = None) -> list[_Row]:
    """Archived conversations — final status `archived` with no live home — for the
    `--all` history block. Mirrors _completed_rows' live-id dedup."""
    from imp.cli.store import _status_rows, make_store
    live = {r[0] for r in _status_rows(control)} if live_ids is None else live_ids
    return [(r.quest_id, r.status, r.step, r.last, r.title, r.created, None, None, None)
            for r in make_store(control).completed_rows()
            if r.status == "archived" and r.quest_id not in live]


def _sort_rows_newest_first(rows: list[_Row]) -> list[_Row]:
    return sorted(rows, key=lambda r: (r[3], r[5]), reverse=True)


def _sort_rows_started_first(rows: list[_Row]) -> list[_Row]:
    """In-flight order: earliest started_at first, falling back to created_at then id."""
    return sorted(rows, key=lambda r: (r[8] or r[5], r[5], r[0]))


def _resumable_among(active: list[_Row], backlog: list[_Row]) -> list[_Row]:
    return _sort_rows_newest_first([r for r in active if r[1] in RESUMABLE] + backlog)


def _resumable_rows(control: Path) -> list[_Row]:
    from imp.cli.store import _backlog_rows, _status_rows
    return _resumable_among(_status_rows(control), _backlog_rows(control))


def _abandon_rows(control: Path) -> list[_Row]:
    from imp.cli.store import _backlog_rows, _status_rows
    return _sort_rows_newest_first(_status_rows(control) + _backlog_rows(control))


@dataclass(frozen=True)
class _UnreadableMatches:
    """Archived ids matching a resume query that this install cannot render,
    paired with the floor text that blocked them — so a caller cannot read the
    rows without confronting that the match set was cut."""
    ids: tuple[str, ...]
    reason: str


def _suppressed_archived_ids(control: Path, token: str) -> list[str]:
    """Archived resumable records matching `token` that have NO live quest home.

    Record enumeration and record reads are ungated by both archive floors, and
    the quest-home test is the one `_row_for_id` branches on — so an id here is
    genuinely unrenderable while a floor is skewed, and one with a live home is
    never suppressed, so a finished quest's leftover home still resolves by its
    full id. The degraded path enumerates records rather than the index: an archive
    below the blob floor is never reindexed, so the record files are the honest
    source."""
    from imp.cli.store import _quest_home
    from imp.engine.archive import record, records_root
    root = records_root(control)
    out: list[str] = []
    for cid in record.list_record_ids(root):
        if token not in cid or (_quest_home(control, cid) / "quest.json").exists():
            continue
        rec = record.read_record(root, cid)
        if rec is not None and rec.get("resumable"):
            out.append(cid)
    return out


def _resumable_matches(
    control: Path, token: str,
) -> tuple[list[_Row], _UnreadableMatches | None]:
    """Rows for resumable quests whose id contains `token`, plus whatever this
    install could not read.

    Live first: a token in the id of any active-board quest (`_status_rows`,
    `_backlog_rows`) returns only the matching resumable rows — empty when no match
    is resumable (e.g. running) — and skips the archive search and floor checks, so
    version skew cannot change the outcome. A finished quest's leftover home is not
    on the board and shadows nothing.

    Any other token spans archived-but-resumable records — an archived id whose
    archive record reads `resumable=True` (a "archived" conversation whose live home
    was deleted). Archived completed/abandoned (non-resumable) ids are never matches,
    so a terminal id sharing a stem never joins the menu. An archived candidate this
    install cannot read is NOT omitted: it is reported to the caller in the second
    element, which is None only when nothing was suppressed."""
    from imp.cli.store import _backlog_rows, _row_for_id, _status_rows, make_store
    from imp.engine.archive import (
        check_index_floor,
        record,
        records_root,
        require_current,
    )
    from imp.engine.archive import read as archive_read
    from imp.engine.errors import ArchiveMismatch

    scanned = make_store(control).scan_homes()
    active = _status_rows(control, scanned)
    backlog = _backlog_rows(control, scanned)
    if any(token in r[0] for r in active + backlog):
        return [r for r in _resumable_among(active, backlog) if token in r[0]], None
    try:
        require_current(control)
        check_index_floor(control)
    except ArchiveMismatch as skew:
        skewed = _suppressed_archived_ids(control, token)
        return [], (_UnreadableMatches(tuple(sorted(skewed)), str(skew))
                    if skewed else None)
    rows: list[_Row] = []
    raced: list[str] = []
    reason: str | None = None
    for cid in archive_read.archived_ids(control):
        if token not in cid:
            continue
        rec = record.read_record(records_root(control), cid)
        if rec is None or not rec.get("resumable"):
            continue
        try:
            row = _row_for_id(control, cid)
        except ArchiveMismatch as race:
            raced.append(cid)
            if reason is None:
                reason = str(race)
            continue
        if row is not None:
            rows.append(row)
    return _sort_rows_newest_first(rows), (
        _UnreadableMatches(tuple(sorted(raced)), reason)
        if reason is not None else None)


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
    return ExitCode.OK


def _picker_cancelled(verb: str) -> ExitCode:
    _emit_cli_error(f"{verb} cancelled")
    return ExitCode.USER_ENDED


def _resume_picker_non_tty(control: Path, rows: list[_Row]) -> str | int:
    if len(rows) == 1:
        return rows[0][0]
    _emit_cli_error("multiple resumable quests — pass an id; candidates:")
    color = should_use_color(sys.stderr)
    now = datetime.now(UTC)
    intents = {r[0]: _row_display_intent(control, r) for r in rows}
    status_w, title_w = quest_row.overview_widths([
        (r[1], quest_row.compose_status_label(r[2], r[6], r[7]),
         quest_row.resolve_display_name(r[4], intents[r[0]]).text) for r in rows])
    print(quest_row.header_line(color=color, title_width=title_w, status_width=status_w),
          file=sys.stderr)
    for r in rows:
        print(quest_row.format_quest_line(
            quest_id=r[0], title=r[4], status=r[1], step=r[2], step_label=r[6],
            phase_label=r[7], last_iso=r[3], now=now, color=color,
            title_width=title_w, status_width=status_w, intent=intents[r[0]]),
            file=sys.stderr)
    return 2


async def _pick_quest_target(
    *, control: Path, rows: list[_Row], header: str,
    on_empty: Callable[[], int],
    on_cancel: Callable[[], int],
    on_non_tty: Callable[[list[_Row]], str | int],
    selected_index: int = 0,
    non_interactive: bool = False,
) -> str | int:
    from imp.cli import resume_select
    if not rows:
        return on_empty()
    if non_interactive or not _interactive():
        return on_non_tty(rows)
    if len(rows) == 1:
        return rows[0][0]
    candidates = [resume_select.Candidate(
        quest_id=r[0], title=r[4], status=r[1], step=r[2], last_iso=r[3],
        step_label=r[6], phase_label=r[7],
        intent=_row_display_intent(control, r))
        for r in rows]
    chosen = await resume_select.run_picker(
        candidates, now=datetime.now(UTC),
        color=should_use_color(sys.stdout),
        header=header, selected_index=selected_index)
    return chosen if chosen is not None else on_cancel()


_RESUME_PICKER_HEADER = (
    "Resume which quest?  (↑/↓ to move · enter to resume · q to cancel)"
)


def _refuse_unreadable_matches(
    token: str, matches: list[_Row], unreadable: _UnreadableMatches,
) -> int:
    """One `imp: ` message: the suppressed ids, the floor text verbatim, and any
    readable match to pass by full id instead."""
    head = ("matches an archived quest this imp cannot read"
            if len(unreadable.ids) == 1
            else "matches archived quests this imp cannot read")
    lines = [f"{token!r} {head}: {', '.join(unreadable.ids)}",
             f"  {unreadable.reason}"]
    readable = sorted(r[0] for r in matches)
    if len(readable) == 1:
        lines.append(f"  hint: readable match {readable[0]} — "
                     "pass its full id to resume it.")
    elif readable:
        lines.append(f"  hint: readable matches {', '.join(readable)} — "
                     "pass a full id to resume one.")
    _emit_cli_error("\n".join(lines))
    return 2


async def _pick_resume_target(control: Path, *, non_interactive: bool) -> str | int:
    return await _pick_quest_target(
        control=control,
        rows=_resumable_rows(control),
        header=_RESUME_PICKER_HEADER,
        on_empty=lambda: _resume_picker_empty(control),
        on_cancel=lambda: _picker_cancelled("resume"),
        on_non_tty=lambda rows: _resume_picker_non_tty(control, rows),
        non_interactive=non_interactive,
    )


async def _resolve_resume_target(
    control: Path, token: str | None, *, non_interactive: bool,
) -> str | int:
    if token is None or not token.strip():
        return await _pick_resume_target(control, non_interactive=non_interactive)
    matches, unreadable = _resumable_matches(control, token)
    if unreadable is not None:
        return _refuse_unreadable_matches(token, matches, unreadable)
    if len(matches) >= 2:
        return await _pick_quest_target(
            control=control,
            rows=matches,
            header=_RESUME_PICKER_HEADER,
            on_empty=lambda: _resume_picker_empty(control),
            on_cancel=lambda: _picker_cancelled("resume"),
            on_non_tty=lambda rows: _resume_picker_non_tty(control, rows),
            non_interactive=non_interactive,
        )
    if len(matches) == 1:
        return matches[0][0]
    try:
        return _resolve_id(control, token)
    except SystemExit:
        return 2


def _deliver_abandon_resident(quest_id: str) -> None:
    import anyio

    from imp.remote.session.client import SessionClient
    from imp.remote.session.discover import session_socket_path
    from imp.remote.session.protocol import CONTROL_ABANDON

    async def _run() -> None:
        path = str(session_socket_path(quest_id))
        client = SessionClient(path, quest_id, "", on_event=lambda _: None)
        await client.connect()
        try:
            await client.send_control(CONTROL_ABANDON)
            with anyio.fail_after(10):
                await client.receive_events()
        finally:
            await client.aclose()

    anyio.run(_run)


def _abandon(control: Path, quest_id: str) -> tuple[int, str]:
    """Tear down a quest (worktree, branch, state) as one logged operation and
    return (rc, message) without printing, so the host CLI and the daemon proxy
    share it (mirrors apply_edit).

    Order is pinned by git (spec §2): archive the runtime snapshot BEFORE
    worktree removal (records live inside the worktree); `git branch -D` AFTER
    (git refuses to delete a branch checked out in a worktree). Idempotent /
    crash-tolerant: re-running after any mid-sequence crash converges."""
    from imp.cli.abandon_confirm import removal_description
    from imp.engine import worktree
    from imp.engine.abandon import tear_down_quest
    from imp.engine.errors import LoadError, LoadErrors
    from imp.engine.quest import Quest
    from imp.engine.state import LockHeld
    from imp.remote.daemon.directory import session_is_resident
    try:
        quest = Quest.load(control, quest_id)
    except (LoadError, LoadErrors):
        # No live or un-materialized quest-home: idempotent teardown — reap a
        # possibly-orphaned branch and exit 0.
        worktree.delete_branch(control, quest_id)
        return 0, f"quest {quest_id} already gone; ensured branch imp/{quest_id} is removed."

    token = quest.id
    removed = removal_description(quest)
    done = (f"Abandoned {token}." if removed is None
            else f"Abandoned {token} · removed {removed}.")
    if session_is_resident(control, quest_id):
        try:
            _deliver_abandon_resident(quest_id)
        except OSError:
            pass  # daemon gone, wedged, or startup-window hang (TimeoutError ⊂ OSError)
        else:
            return 0, done
    try:
        tear_down_quest(quest)
    except LockHeld as e:
        return 2, (f"imp: quest {quest_id} is running (pid {e.held_by_pid or '?'}); "
                   f"stop it before abandoning")
    return 0, done


def _abandon_local(control: Path, quest_id: str) -> int:
    """Local (host-side) path: run the teardown and print the result —
    stdout via _emit_human (adds a trailing blank on a tty) on rc 0, stderr on non-zero."""
    rc, message = _abandon(control, quest_id)
    if rc == 0:
        _emit_human([message])
    else:
        print(message, file=sys.stderr)
    return rc


def _cmd_remote_control(args: argparse.Namespace) -> int:
    args.parser.print_help(sys.stderr)
    return 2


def _cmd_remote_control_start(args: argparse.Namespace) -> int:
    from imp.remote.daemon.runner import start_daemon
    return start_daemon(_control_root())


def _cmd_remote_control_stop(args: argparse.Namespace) -> int:
    from imp.remote.daemon.runner import stop_daemon
    return stop_daemon(_control_root())


def _abandon_picker_empty() -> int:
    _emit_human(["Nothing to abandon."])
    return ExitCode.OK


def _abandon_picker_non_tty(rows: list[_Row]) -> int:
    _emit_cli_error("abandon needs a quest id on a non-interactive terminal")
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
        on_cancel=lambda: _picker_cancelled("abandon"),
        on_non_tty=_abandon_picker_non_tty,
    )


async def _pick_abandon_matches(
    control: Path, token: str, rows: list[_Row],
) -> str | int:
    """Scoped abandon picker over the token's live matches. On a non-tty it
    errors with a token-specific message (distinct from the no-token
    `_abandon_picker_non_tty` copy, which reads wrong when a token was given)."""
    def _non_tty(_rows: list[_Row]) -> int:
        _emit_cli_error(f"{token!r} matches multiple quests; pass more of the id")
        return 2

    return await _pick_quest_target(
        control=control,
        rows=rows,
        header=_ABANDON_PICKER_HEADER,
        on_empty=_abandon_picker_empty,
        on_cancel=lambda: _picker_cancelled("abandon"),
        on_non_tty=_non_tty,
    )


def _resolve_abandon_target(control: Path, token: str | None) -> str | int:
    import anyio

    from imp.engine.archive import read as archive_read
    if token is None or not token.strip():
        return anyio.run(_pick_abandon_target, control)
    matches = _abandon_match_rows(control, token)
    if len(matches) >= 2:
        return anyio.run(_pick_abandon_matches, control, token, matches)
    if len(matches) == 1:
        return matches[0][0]
    try:
        return _resolve_id(control, token)
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
            return hits[0]
        # Truly unknown (never existed, or ambiguous) — nothing to reap.
        _emit_cli_error(f"no quest matches {token!r}")
        return 2


def _abandon_approved(control: Path, quest_id: str) -> bool:
    from imp.cli.abandon_confirm import abandon_question, removal_description
    from imp.engine.errors import LoadError, LoadErrors
    from imp.engine.quest import Quest
    try:
        quest = Quest.load(control, quest_id)
    except (LoadError, LoadErrors):
        return True
    removed = removal_description(quest)
    if removed is None:
        return True
    # __main__'s SIGINT handler exits 130; Ctrl-C here must decline instead.
    prev = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        answer = input(abandon_question(quest.id, quest.title, removed))
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    finally:
        signal.signal(signal.SIGINT, prev)
    return answer.strip().lower() in ("y", "yes")


def _cmd_abandon(args: argparse.Namespace) -> int:
    from imp.cli.mutation_proxy import proxy_mutation
    token = args.token
    if os.environ.get("IMP_SESSION_SOCKET"):
        _refuse_foreign_cwd()
        if token is None or not token.strip():
            _emit_cli_error("abandon needs a quest id in a sandboxed session")
            return 2
        return _emit_mutation_result(proxy_mutation("abandon", {"token": token}))
    control = _control_root()
    target = _resolve_abandon_target(control, token)
    if isinstance(target, int):
        return target
    if not args.yes and _interactive() and not _abandon_approved(control, target):
        _emit_human([f"Kept {target}."])
        return ExitCode.USER_ENDED
    return _abandon_local(control, target)


_RESIDENT_RENAME_TIMEOUT = 10.0


def _deliver_rename_resident(quest_id: str, title: str) -> None:
    import anyio

    from imp.cli import store_proxy
    from imp.remote.session.discover import session_socket_path

    async def _run() -> None:
        with anyio.fail_after(_RESIDENT_RENAME_TIMEOUT):
            await store_proxy.roundtrip(
                str(session_socket_path(quest_id)), QUERY_USER_RENAME, {"title": title})

    anyio.run(_run)


def _renamed_by_resident(control: Path, quest_id: str, title: str) -> bool:
    """True iff quest_id's resident session daemon applied the rename to its own quest."""
    from imp.remote.daemon.directory import session_is_resident
    if not session_is_resident(control, quest_id):
        return False
    try:
        _deliver_rename_resident(quest_id, title)
    except Exception:  # IPC boundary: any failure falls back to the local write
        return False
    return True


def apply_edit(
    control: Path, quest_id: str, *, title: str | None, backlog: bool,
    active: bool, depends_on: list[str] | None, clear_deps: bool,
    append_intent: str | None, base: str | None,
) -> tuple[int, str | None]:
    """Return (rc, message) for an `imp edit` mutation so host CLI and daemon proxy share it."""
    from imp.engine import worktree
    from imp.engine.quest import Quest
    quest = Quest.load(control, quest_id)

    zone_change = backlog or active
    dep_change = depends_on is not None or clear_deps
    if (title is None and not zone_change and not dep_change
            and append_intent is None and base is None):
        return 2, ("imp: imp edit needs at least one of --title, --backlog/--active, "
                   "--depends-on/--clear-deps, --base, or --append-intent")

    if zone_change and quest.materialized:
        return 2, (f"imp: {quest_id} has already started building, so it can't "
                   "move between the active list and the backlog")

    if dep_change and quest.materialized:
        return 2, (f"imp: {quest_id} has already started building, so its "
                   "dependencies can't change")

    if base is not None:
        if quest.materialized:
            return 2, (f"imp: {quest_id} has already started building, so its "
                       "base branch can't change")
        if not worktree.branch_exists(control, base):
            return 2, f"imp: --base {base!r} does not name an existing branch"

    if append_intent is not None:
        conversed = _first_user_message(list(quest.event_log().read())) is not None
        if quest.materialized or conversed:
            return 2, (f"imp: {quest_id} has already started; --append-intent only "
                       "applies before a quest is picked up")

    prior_origin = quest.origin
    prior_deps = list(quest.depends_on)
    title_changed = title is not None and title != quest.title
    new_base = base if base is not None and base != quest.base_branch else None

    if (title is not None and title_changed
            and not _renamed_by_resident(control, quest_id, title)):
        quest = Quest.load(control, quest_id)  # a failed delivery may still have applied it
        if quest.materialized:
            quest.rename(title, by="user", log=quest.event_log())
        else:
            quest.set_title(title)
    if backlog:
        quest.set_origin("backlog")
    elif active:
        quest.set_origin("draft")
    if clear_deps:
        quest.set_depends_on([])
    elif depends_on is not None:
        quest.set_depends_on(depends_on)
    if append_intent is not None:
        existing = quest.seed_opening or ""
        if existing:
            quest.set_seed_opening(existing + "\n\n" + append_intent)
        else:
            quest.set_seed_opening(append_intent)
    if new_base is not None:
        quest.set_base_branch(new_base)

    fragments: list[str] = []
    if title_changed:
        fragments.append(f'title → "{title}"')
    if backlog and prior_origin != "backlog":
        fragments.append("moved to backlog")
    elif active and prior_origin != "draft":
        fragments.append("moved to active")
    if depends_on is not None and depends_on and depends_on != prior_deps:
        fragments.append(f'depends on {", ".join(depends_on)}')
    elif (clear_deps or (depends_on is not None and not depends_on)) and prior_deps:
        fragments.append("cleared dependencies")
    if new_base is not None:
        checkout = worktree.checked_out_branch(control)
        diverged = (f" (main checkout is on {checkout})"
                    if checkout is not None and checkout != new_base else "")
        fragments.append(f"base → {new_base}{diverged}")
    if append_intent is not None:
        fragments.append("appended to intent")

    changes = "; ".join(fragments) if fragments else "no changes"
    return 0, f"edited {quest_id} · {changes}"


def _refuse_foreign_cwd() -> None:
    from imp.cli.store import refuse_foreign_project
    from imp.engine import worktree
    try:
        cwd_root = worktree.find_control_root(Path.cwd())
    except worktree.PreconditionError:
        return  # outside any repo: nothing foreign to protect
    refuse_foreign_project(cwd_root)


def _emit_mutation_result(result: dict[str, Any]) -> int:
    rc: int = result["returncode"]
    message = result.get("message")
    if message:
        print(message, file=sys.stderr if rc else sys.stdout)
    return rc


def _cmd_edit(args: argparse.Namespace) -> int:
    from imp.cli.mutation_proxy import proxy_mutation
    if os.environ.get("IMP_SESSION_SOCKET"):
        _refuse_foreign_cwd()
        return _emit_mutation_result(proxy_mutation("edit", {
            "token": args.token, "title": args.title, "backlog": args.backlog,
            "active": args.active, "depends_on": args.depends_on,
            "clear_deps": args.clear_deps, "append_intent": args.append_intent,
            "base": args.base}))
    control = _control_root()
    quest_id = _resolve_id(control, args.token)
    rc, msg = apply_edit(
        control, quest_id, title=args.title, backlog=args.backlog,
        active=args.active, depends_on=args.depends_on,
        clear_deps=args.clear_deps, append_intent=args.append_intent,
        base=args.base)
    if msg:
        print(msg, file=sys.stderr if rc else sys.stdout)
    return rc


_HISTORY_CAP = 10

_SINCE_ERROR = (
    "--since expects 'today' or a number with a unit — "
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


_DEFAULT_LIST_DISPOSITIONS = frozenset({"active", "backlog", "completed"})


def _board_dispositions(*, show_all: bool) -> set[str]:
    """The unfiltered board's zones: the defaults, plus abandoned/archived under --all."""
    picked = set(_DEFAULT_LIST_DISPOSITIONS)
    if show_all:
        picked |= {"abandoned", "archived"}
    return picked


def _selected_list_dispositions(args: argparse.Namespace) -> set[str]:
    """Union of the disposition tokens selected by the `imp list` flags."""
    picked: set[str] = set()
    if args.active:
        picked.add("active")
    if args.backlog:
        picked.add("backlog")
    if args.completed:
        picked.add("completed")
    if args.abandoned:
        picked.add("abandoned")
    if args.archived:
        picked.add("archived")
    return picked


def _history_by_disposition(control: Path, *, dispositions: set[str],
                            live_ids: set[str] | None) -> list[_Row]:
    """History-zone rows for the requested dispositions, newest-first."""
    wanted = dispositions & {"completed", "abandoned", "archived"}
    rows = _completed_rows(control, live_ids) + _archived_rows(control, live_ids)
    return _sort_rows_newest_first([r for r in rows if r[1] in wanted])



def _overview_body(inflight: list[_Row], backlog_rows: list[_Row],
                   history: list[_Row], *, intents: dict[str, str | None],
                   history_label: str, inflight_notice: bool, now: datetime,
                   color: bool) -> list[str]:
    """Render the sectioned overview for already-gathered rows."""

    def _name_text(r: _Row) -> str:
        return quest_row.resolve_display_name(r[4], intents.get(r[0])).text

    specs: list[tuple[str, str | None, str]] = []
    for r in inflight:
        specs.append((r[1], quest_row.compose_status_label(r[2], r[6], r[7]),
                      _name_text(r)))
    for r in backlog_rows:
        specs.append((r[1], quest_row.compose_status_label(r[2], r[6], None),
                      _name_text(r)))
    for r in history:
        specs.append((r[1], None, _name_text(r)))

    status_width, title_width = quest_row.overview_widths(specs)

    header = quest_row.header_line(color=color, title_width=title_width,
                                   status_width=status_width)
    lines: list[str] = []
    for r in inflight:
        lines.append(quest_row.format_quest_line(
            quest_id=r[0], title=r[4], status=r[1], step=r[2], step_label=r[6],
            phase_label=r[7], last_iso=r[3], now=now, color=color,
            title_width=title_width, status_width=status_width,
            intent=intents.get(r[0])))
    if backlog_rows:
        if lines:
            lines.append("")
        lines.append(pal.ansi("backlog:", "dim", enabled=color))
        for r in backlog_rows:
            lines.append(quest_row.format_quest_line(
                quest_id=r[0], title=r[4], status=r[1], step=r[2],
                step_label=r[6], phase_label=None, last_iso=r[3], now=now,
                color=color, title_width=title_width, status_width=status_width,
                intent=intents.get(r[0])))
    if history:
        if lines:
            lines.append("")
        lines.append(pal.ansi(history_label, "dim", enabled=color))
        for r in history:
            lines.append(quest_row.format_quest_line(
                quest_id=r[0], title=r[4], status=r[1], step="-",
                last_iso=r[3], now=now, color=color, title_width=title_width,
                status_width=status_width, intent=intents.get(r[0])))
    if inflight_notice and not inflight:
        return [pal.ansi("no active quests", "dim", enabled=color), "", header, *lines]
    return [header, *lines]


def _overview_lines(control: Path, *, show_all: bool, now: datetime,
                    color: bool) -> list[str]:
    """Overview lines across all zones, sharing one content-sized STATUS/TITLE width."""
    rows = _overview_rows(control, dispositions=_board_dispositions(show_all=show_all),
                          cutoff=None)
    if not rows.inflight and not rows.backlog and not rows.history:
        if not show_all and _history_by_disposition(
                control, dispositions={"abandoned", "archived"},
                live_ids=set()):
            return [_NO_ACTIVE_QUESTS_MESSAGE]
        return [_NO_QUESTS_MESSAGE]
    label = "history:" if show_all else "completed:"
    return _overview_body(rows.inflight, rows.backlog, rows.history,
                          intents=rows.display_intents, history_label=label,
                          inflight_notice=True, now=now, color=color)


_NO_FILTER_MATCH_MESSAGE = "No quests match that filter."


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


class _OverviewRows(NamedTuple):
    inflight: list[_Row]
    backlog: list[_Row]
    history: list[_Row]
    display_intents: dict[str, str | None]


def _overview_rows(control: Path, *, dispositions: set[str],
                   cutoff: datetime | None) -> _OverviewRows:
    """Board rows for the requested dispositions, zone-sorted and cutoff-filtered,
    with the display intents title-less rows fall back to."""
    from imp.cli.store import _backlog_rows, _meta_seed_opening, _status_rows, make_store
    scanned = make_store(control).scan_homes()
    status_rows = _status_rows(control, scanned)
    live_ids = {r[0] for r in status_rows}
    inflight = (_sort_rows_started_first(status_rows)
                if "active" in dispositions else [])
    backlog = (_sort_rows_newest_first(_backlog_rows(control, scanned))
               if "backlog" in dispositions else [])
    history = _history_by_disposition(control, dispositions=dispositions,
                                      live_ids=live_ids)
    inflight = [r for r in inflight if _row_since(r, cutoff)]
    backlog = [r for r in backlog if _row_since(r, cutoff)]
    history = [r for r in history if _row_since(r, cutoff)]
    display_intents: dict[str, str | None] = {
        d.quest_id: quest_row.fallback_intent(d.intent_title, d.meta.get("seed_opening"))
        for d in scanned}
    for r in history:
        if not r[4].strip() and r[0] not in display_intents:
            display_intents[r[0]] = quest_row.fallback_intent(
                _resolve_row_intent(control, r), _meta_seed_opening(control, r[0]))
    return _OverviewRows(inflight, backlog, history, display_intents)


def _filtered_overview_lines(control: Path, *, dispositions: set[str],
                              cutoff: datetime | None, now: datetime,
                              color: bool) -> list[str]:
    """Sectioned overview over the full uncapped set, filtered by disposition and cutoff."""
    rows = _overview_rows(control, dispositions=dispositions, cutoff=cutoff)
    if not rows.inflight and not rows.backlog and not rows.history:
        return [_NO_FILTER_MATCH_MESSAGE]
    return _overview_body(rows.inflight, rows.backlog, rows.history,
                          intents=rows.display_intents, history_label="history:",
                          inflight_notice="active" in dispositions and cutoff is None,
                          now=now, color=color)


def _board_entry_json(r: _Row, *, zone: Zone,
                      status: str, display_intent: str | None) -> dict[str, Any]:
    """One board entry's always-present keys. `status` is a parameter because a
    backlog row's slot 1 holds its origin word, not a status."""
    title = r[4].strip()
    name = quest_row.resolve_display_name(title, display_intent)
    return {
        "id": r[0],
        "zone": zone,
        "title": title or None,
        "name": None if name.kind is quest_row.NameKind.UNTITLED else name.text,
        "status": status,
        "created": r[5] or None,
        "last_activity": r[3] or None,
    }


def _board_json(control: Path, *, dispositions: set[str],
                cutoff: datetime | None) -> list[dict[str, Any]]:
    """The board as JSON entries in display order: in-flight, backlog, history."""
    rows = _overview_rows(control, dispositions=dispositions, cutoff=cutoff)
    entries: list[dict[str, Any]] = []
    for r in rows.inflight:
        entry = _board_entry_json(r, zone="active", status=r[1],
                                  display_intent=rows.display_intents.get(r[0]))
        if r[2] not in ("", "-"):
            entry["step"] = {"phase": r[7] or None, "name": r[6] or r[2]}
        entries.append(entry)
    for r in rows.backlog:
        entry = _board_entry_json(r, zone="backlog", status="backlog",
                                  display_intent=rows.display_intents.get(r[0]))
        entry["provenance"] = r[1]
        entry["readiness"] = "blocked" if r[2] == "blocked" else "ready"
        entries.append(entry)
    for r in rows.history:
        entries.append(_board_entry_json(
            r, zone="history", status=r[1],
            display_intent=rows.display_intents.get(r[0])))
    return entries


_SEARCH_ZONES: tuple[Zone, ...] = ("active", "backlog", "history")


def _selected_search(args: argparse.Namespace, merged_after: str | None) -> SearchZones:
    """Zones from the five zone flags; none searches everything, merged_after implies completed."""
    from imp.cli.store import SearchZones
    statuses = frozenset(
        status for status, on in (("completed", args.completed),
                                  ("abandoned", args.abandoned),
                                  ("archived", args.archived)) if on)
    if merged_after is not None:
        statuses |= {"completed"}
    if not (args.active or args.backlog or statuses):
        return SearchZones(True, True, frozenset({"completed", "abandoned", "archived"}),
                           merged_after)
    return SearchZones(args.active, args.backlog, statuses, merged_after)


def _widening_flags(zone_flags: list[tuple[str, bool]],
                    filter_flags: list[tuple[str, bool]]) -> list[str]:
    """Passed flags a miss could drop to widen; zone flags omitted when all are passed."""
    zones = [flag for flag, on in zone_flags if on]
    if len(zones) == len(zone_flags):
        zones = []
    return zones + [flag for flag, on in filter_flags if on]


def _search_miss_lines(query: str, drop: list[str], *,
                       noun: Literal["quests", "events"], suggest_events: bool) -> list[str]:
    if drop:
        hint = f"Try another term, or drop {' '.join(drop)} to widen the search."
    elif suggest_events:
        hint = "Try another term, or `imp events search` to include tool calls and results."
    else:
        hint = "Try another term."
    return [f'No {noun} match "{query}".',
            pal.ansi(hint, "dim", enabled=should_use_color(sys.stdout))]


def _matched_events(
    events: list[dict[str, Any]], query: str, *,
    types: set[str] | None, agents: set[str] | None,
) -> tuple[list[tuple[int, dict[str, Any]]], dict[str, str], dict[int, dict[str, Any]]]:
    """Matched (ordinal, event) pairs after type/agent filters, plus the handle map and overlays."""
    from imp.cli.render_helpers import event_text
    from imp.engine import events_index as ei
    indexed = list(enumerate(events, start=1))
    handle_map = ei.subagent_handle_map(indexed)
    # overlays must be computed over full indexed list; patch row may not be in matched subset
    all_overlays = ei.usage_patch_overlays(indexed)
    candidates = ei.select_event_lines(
        indexed, types=types, agents=agents, handle_map=handle_map)
    q = query.lower()
    matched = [(o, ev) for o, ev in candidates if q in event_text(ev).lower()]
    matched_ordinals = {o for o, _ in matched}
    overlays = {o: v for o, v in all_overlays.items() if o in matched_ordinals}
    return matched, handle_map, overlays


class EventSearchHit(NamedTuple):
    quest_id: str
    title: str
    zone: Zone
    last_activity: str | None
    events: list[tuple[int, dict[str, Any]]]
    handle_map: dict[str, str]
    match_count: int
    overlays: dict[int, dict[str, Any]]
    intent: str | None   # titleless name source (opening, else seed_opening); None when titled


def _events_search_active(
    control: Path, query: str, *, types: set[str] | None, agents: set[str] | None,
) -> list[EventSearchHit]:
    """In-flight quests with >=1 event matching query (after type/agent filters)."""
    from imp.cli.store import _resolve_intent, _status_rows, make_store
    store = make_store(control)
    scanned = store.scan_homes()
    seed_by_id = {d.quest_id: d.meta.get("seed_opening") for d in scanned}
    hits: list[EventSearchHit] = []
    for r in _status_rows(control, scanned):
        events = store.get_events(r[0]) or []
        matched, handle_map, overlays = _matched_events(events, query, types=types, agents=agents)
        if matched:
            intent = None if r[4].strip() else quest_row.fallback_intent(
                _resolve_intent(events), seed_by_id.get(r[0]))
            hits.append(EventSearchHit(
                r[0], r[4], "active", r[3], matched, handle_map, len(matched), overlays,
                intent))
    return hits


def _events_search_backlog(
    control: Path, query: str, *, types: set[str] | None, agents: set[str] | None,
) -> list[EventSearchHit]:
    """Backlog quests matched on id/title/intent. Skipped when type/agent filter is set."""
    if types is not None or agents is not None:
        return []
    from imp.cli.store import _backlog_matches
    return [EventSearchHit(m.quest_id, m.title, "backlog", m.last_activity, [], {}, 1, {},
                           None if m.title.strip() else quest_row.fallback_intent(None, m.seed))
            for m in _backlog_matches(control, query)]


def _archived_hit_intent(store: Store, quest_id: str,
                         events: list[dict[str, Any]]) -> str | None:
    """A titleless archived hit's name source; its card loads only when the events carry none."""
    from imp.cli.store import _resolve_intent
    if (intent := _resolve_intent(events)) is not None:
        return intent
    card = store.quest_card(quest_id)
    return quest_row.fallback_intent(None, card.seed_opening if card is not None else None)


def _events_search_archived(
    control: Path, query: str, *, types: set[str] | None, agents: set[str] | None,
    statuses: frozenset[str],
) -> list[EventSearchHit]:
    """History-zone quests in `statuses`: index candidates re-scanned to matching events."""
    from imp.cli.store import make_store
    store = make_store(control)
    hits: list[EventSearchHit] = []
    for cid, title, status, last in store.search_event_candidates(query):
        if status not in statuses:
            continue
        events = store.get_events(cid) or []
        matched, handle_map, overlays = _matched_events(events, query, types=types, agents=agents)
        if matched:
            intent = None if title.strip() else _archived_hit_intent(store, cid, events)
            hits.append(EventSearchHit(
                cid, title, "history", last, matched, handle_map, len(matched), overlays,
                intent))
    return hits


def _events_search(
    control: Path, query: str, zones: SearchZones, *,
    types: set[str] | None, agents: set[str] | None,
) -> list[EventSearchHit]:
    hits: list[EventSearchHit] = []
    if zones.active:
        hits += _events_search_active(control, query, types=types, agents=agents)
    if zones.backlog:
        hits += _events_search_backlog(control, query, types=types, agents=agents)
    if zones.statuses:
        hits += _events_search_archived(control, query, types=types, agents=agents,
                                        statuses=zones.statuses)
    return hits


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


def _search_name_segment(title: str, intent: str | None, query: str, *,
                         color: bool) -> str | None:
    """A search heading's name: a real title plain, else the dim TITLE_CAP-clipped fallback."""
    resolved = quest_row.resolve_display_name(title, intent)
    if resolved.kind is quest_row.NameKind.UNTITLED:
        return None
    if resolved.kind is quest_row.NameKind.TITLE:
        return _spine_segment(resolved.text, query, color=color)
    return _spine_segment(quest_row._truncate(resolved.text, quest_row.TITLE_CAP),
                          query, "dim", color=color)


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
        prefix = "…" if start > 0 else ""
        suffix = "…" if end < len(content) else ""
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
    """Spine heading id · name · zone · age; query highlighted; name dropped when absent."""
    id_seg = _spine_segment(hit.quest_id, query, *pal.NODE_COLORS["quest_id"],
                            color=color)
    age = quest_row.relative_age(hit.last_activity, now=now)
    tail = pal.ansi(f"{hit.zone} · {age}", "dim", enabled=color)
    name = _search_name_segment(hit.title, hit.intent, query, color=color)
    if name is None:
        return f"{id_seg} · {tail}"
    return f"{id_seg} · {name} · {tail}"


def _cmd_search(args: argparse.Namespace, _control: Path | None = None) -> int:
    from imp.cli.store import make_store
    control = _control if _control is not None else _control_root()
    zones = _selected_search(args, args.merged_after)
    hits: list[SearchHit] = list(make_store(control).search(args.query, zones))
    if not hits:
        zone_flags = [("--active", args.active), ("--backlog", args.backlog),
                      ("--completed", args.completed), ("--abandoned", args.abandoned),
                      ("--archived", args.archived)]
        drop = _widening_flags(zone_flags, [("--merged-after", args.merged_after is not None)])
        narrowed = any(on for _, on in zone_flags) or args.merged_after is not None
        _emit_human(_search_miss_lines(args.query, drop, noun="quests",
                                       suggest_events=not narrowed))
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
    name = _search_name_segment(hit.title, hit.intent, query, color=color)
    if name is None:
        return f"{id_seg} · {tail}"
    return f"{id_seg} · {name} · {tail}"


def _render_events_search_hit(
    hit: EventSearchHit, query: str, *, now: datetime, color: bool,
    width: int, limit: int | None = 3,
) -> list[str]:
    from imp.cli.store import SearchHit
    from imp.engine import events_index as ei
    spine = _search_list_line(
        SearchHit(hit.quest_id, hit.title, hit.zone, "", hit.last_activity, hit.intent),
        query=query, now=now, color=color)
    lines = [spine]
    events = hit.events if limit is None else hit.events[:limit]
    for ordinal, event in events:
        digest = ei.digest_line(ordinal, event, hit.handle_map, width=width,
                                overlay=hit.overlays.get(ordinal))
        lines.append("  " + _highlight(digest, query, color=color))
    extra = len(hit.events) - len(events)
    if limit is not None and extra > 0:
        lines.append(pal.ansi(f"  … +{extra} more", "dim", enabled=color))
    return lines


def _events_search_json(
    hits: list[EventSearchHit], *, event_limit: int | None = 3,
) -> list[dict[str, Any]]:
    from imp.engine import events_index as ei
    return [
        {"quest_id": h.quest_id, "title": h.title, "zone": h.zone,
         "last_activity": h.last_activity,
         "events": ei.events_json(
             h.events if event_limit is None else h.events[:event_limit])}
        for h in hits
    ]


def _events_search_output_control_error(args: argparse.Namespace) -> str | None:
    if args.limit is not None and args.limit <= 0:
        return "--limit must be a positive integer"
    if args.all and args.limit is not None:
        return "--all and --limit cannot be used together"
    return None


def _cmd_events_search(
    args: argparse.Namespace, _control: Path | None = None,
) -> int:
    if args.query is None:
        _emit_cli_error("events search needs a query — see imp events search -h")
        return 2
    if error := _events_search_output_control_error(args):
        _emit_cli_error(error)
        return 2
    control = _control if _control is not None else _control_root()
    types = set(args.type) if args.type else None
    agents = set(args.agent) if args.agent else None
    zones = _selected_search(args, None)
    hits = _events_search(control, args.query, zones, types=types, agents=agents)
    if not hits:
        if args.json:
            _page_or_print(["[]"], no_pager=args.no_pager)
        else:
            drop = _widening_flags(
                [("--active", args.active), ("--backlog", args.backlog),
                 ("--completed", args.completed), ("--abandoned", args.abandoned),
                 ("--archived", args.archived)],
                [("--type", bool(args.type)), ("--agent", bool(args.agent))])
            _emit_human(_search_miss_lines(args.query, drop, noun="events",
                                           suggest_events=False))
        return 0
    color = should_use_color(sys.stdout)
    zone_order = {z: i for i, z in enumerate(_SEARCH_ZONES)}
    hits.sort(key=lambda h: h.last_activity or "", reverse=True)
    hits.sort(key=lambda h: zone_order[h.zone])
    if args.all or (args.list and not args.json and args.limit is None):
        quest_limit = None
    else:
        quest_limit = args.limit if args.limit is not None else 3
    event_limit = None if args.all else 3
    selected_hits = hits[:quest_limit]
    if args.json:
        _page_or_print(
            json.dumps(_events_search_json(
                selected_hits, event_limit=event_limit), indent=2).splitlines(),
            no_pager=args.no_pager)
        return 0
    if args.list:
        lines = [_events_search_list_line(h, query=args.query, color=color)
                 for h in selected_hits]
        _page_or_print(lines, no_pager=args.no_pager, trailing_blank=True)
        return 0
    now = datetime.now(UTC)
    total = sum(h.match_count for h in hits)
    quests = len(hits)
    noun_m = "match" if total == 1 else "matches"
    noun_q = "quest" if quests == 1 else "quests"
    header = pal.ansi(f'{total} {noun_m} in {quests} {noun_q} for "{args.query}"',
                      "dim", enabled=color)
    width = _term_width()
    lines = [header]
    for h in selected_hits:
        lines.append("")
        lines.extend(_render_events_search_hit(
            h, args.query, now=now, color=color, width=width,
            limit=event_limit))
    extra = len(hits) - len(selected_hits)
    if extra > 0:
        lines.append("")
        lines.append(pal.ansi(f"… +{extra} more quests", "dim", enabled=color))
    _page_or_print(lines, no_pager=args.no_pager, trailing_blank=True)
    return 0


def _cmd_config(args: argparse.Namespace) -> int:
    from imp.cli.config_cmd import render_config, render_explain
    from imp.providers.config import _REQUIRED_TIERS
    from imp.providers.effective import load_effective_config
    explain = getattr(args, "explain", None)
    if explain is not None and explain not in _REQUIRED_TIERS:
        _emit_cli_error(f"unknown tier {explain!r} "
                        f"(choose from: {', '.join(_REQUIRED_TIERS)})")
        return 2
    root = _claiming_settings_root()
    eff = load_effective_config(control_root=root)
    color = should_use_color(sys.stdout)
    buf = io.StringIO()
    if explain is not None:
        render_explain(eff, explain, buf, color=color)
    else:
        render_config(eff, buf,
                      state_dir=None if root is None else project_state_dir(root),
                      color=color, show_all=args.all)
    _emit_human(buf.getvalue().splitlines())
    return 0


def _cmd_config_set(args: argparse.Namespace) -> int:
    from imp.cli.config_write import run_set
    return run_set(key=args.key, value=args.value, scope=args.scope,
                   control_root=_settings_root())


def _cmd_config_unset(args: argparse.Namespace) -> int:
    from imp.cli.config_write import run_unset
    return run_unset(key=args.key, scope=args.scope,
                     control_root=_settings_root())


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
    from imp.cli.store import make_store
    from imp.engine import events_index as ei

    if args.token is None:
        _emit_missing_quest_id("events")
        return 2
    control = _control_root()
    try:
        quest_id = _resolve_id(control, args.token)
    except SystemExit:
        return 2
    meta = _unstarted_backlog_meta(control, quest_id)
    if meta is not None:
        _emit_human([f"Quest {quest_id} hasn't started yet — no events to show."])
        return 0
    events = make_store(control).get_events(quest_id)
    if events is None:
        _emit_cli_error(f"no such quest {quest_id!r}")
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
        _emit_cli_error(f"{exc}")
        return 2

    if steps is not None and _reject_unknown_step(steps, indexed, quest_id):
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
    overlays = ei.usage_patch_overlays(indexed)
    _page_or_print(
        [ei.digest_line(o, ev, handle_map, width=width, overlay=overlays.get(o))
         for o, ev in selected],
        no_pager=args.no_pager, trailing_blank=True)
    return 0


def _build_events_search_parser() -> argparse.ArgumentParser:
    p = ImpParser(
        prog="imp events search",
        description=(
            "Case-insensitive substring search over structured event content "
            "(tool calls, tool results, agent reasoning, skill loads) across "
            "quests — the structured-event sibling of `imp search` (prose only). "
            "--active, --backlog, --completed, --abandoned, and --archived narrow "
            "the search and combine; with none of them, everything is searched. "
            "--type / --agent narrow which events match."),
    )
    p.add_argument(
        "query", metavar="QUERY", nargs="?", default=None,
        help="Substring matched case-insensitively against event content.")
    p.add_argument("--active", action="store_true",
                   help="Search in-flight quests' events.")
    p.add_argument("--backlog", action="store_true",
                   help="Search backlog quests (matched on title + intent).")
    p.add_argument("--completed", action="store_true",
                   help="Search completed quests' events.")
    p.add_argument("--abandoned", action="store_true",
                   help="Search abandoned quests' events.")
    p.add_argument("--archived", action="store_true",
                   help="Search archived conversations' events.")
    p.add_argument(
        "--type", action=SplitAppend, default=None,
        help="Keep only these event types (repeatable / comma-joined).")
    p.add_argument(
        "--agent", action=SplitAppend, default=None,
        help="Keep only these authors: step name, subagent handle, or sub:<id> "
             "(repeatable / comma-joined).")
    p.add_argument(
        "--limit", type=int, default=None,
        help="Show at most LIMIT matched quests (default: 3, except with --list).")
    p.add_argument(
        "--all", action="store_true",
        help="Show every matched quest and matching event; conflicts with --limit.")
    p.add_argument(
        "-l", "--list", action="store_true", dest="list",
        help="One terse line per matched quest (id, title, zone); combine with --limit to cap.")
    p.add_argument(
        "--json", action="store_true",
        help="Structured output, bounded like the default view unless --all.")
    p.add_argument("--no-pager", action="store_true",
                   help="Disable the pager even when stdout is a TTY.")
    p.set_defaults(func=_cmd_events_search)
    return p


_CONFIG_KEY_HELP = "Dotted setting name, e.g. theme or tiers.high.anthropic."


def _add_config_scope(parser: argparse.ArgumentParser) -> None:
    """--project / --global / --local as a store_const into `scope`
    (`args.global` would be a syntax error)."""
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--project", dest="scope", action="store_const",
                       const="project",
                       help="Write to imp/config.yaml (this repo).")
    group.add_argument("--global", dest="scope", action="store_const",
                       const="global",
                       help="Write to ~/.imp/config.yaml (this machine).")
    group.add_argument("--local", dest="scope", action="store_const",
                       const="local",
                       help="Write to ~/.imp/projects/<name>/config.yaml "
                            "(this checkout).")
    parser.set_defaults(scope=None)


def _build_parser() -> argparse.ArgumentParser:
    parser = ImpParser(
        prog="imp",
        description=TAGLINE,
        epilog=("New to a project? Run 'imp init' to get started.\n"
                "Run 'imp <command> -h' for a command's options."),
        formatter_class=ImpTopLevelHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"imp {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=False, metavar="<command>")

    p_init = sub.add_parser(
        "init",
        help="Set up imp here (initializes git if needed).",
        description=(
            "Set up imp in this directory: optionally initialize a git\n"
            "repository, write imp/config.yaml (the version-controlled\n"
            "workflow + project settings) with the default workflow, detect\n"
            "your terminal theme, and bootstrap your machine-local config\n"
            "(providers and theme). Makes an initial commit of\n"
            "imp/config.yaml, the imp/memory/ scaffold, and .gitignore."
        ),
    )
    p_init.set_defaults(func=_cmd_init, warm=True)
    p_init.add_argument(
        "--use-defaults", action="store_true", dest="non_interactive",
        help="Non-interactive setup: write imp/config.yaml with the default "
             "workflow, resolved integration branch, and explanatory comments — "
             "no model call, works in CI with no credentials.")
    p_init.add_argument(
        "--providers", metavar="CSV", default=None,
        help="Comma-separated providers to enable, in preference order; skips "
             "the picker. Defaults to all four — the first authenticated one "
             "is used.")
    p_init.add_argument(
        "--workflow", metavar="NAME", default=None,
        help="Default workflow recorded by the non-interactive path (defaults "
             "to `default`); must name an available workflow — an unknown name "
             "is rejected in either mode.")
    p_init.add_argument(
        "--base", metavar="BRANCH", default=None,
        help="Integration branch to record as default-base in the "
             "non-interactive path (otherwise resolved from git).")
    p_init.add_argument(
        "--pythonpath", metavar="CSV", default=None,
        help="Comma-separated worktree-relative Python import paths recorded as "
             "pythonpath in the non-interactive path; validated against disk.")

    p_new = sub.add_parser(
        "new", help="Start a new quest: run the workflow from an opening intent.",
        description=(
            "Start a new quest and run the workflow from your opening message. "
            "By default imp scopes the work with you first, and the quest stays "
            "a draft with no branch until you choose to build it. Once you've "
            "replied, a draft you leave stays in your active list, ready for "
            "imp resume <id>. Use --backlog to save an idea for later, or "
            "--background to run the quest detached with no live prompt."))
    p_new.add_argument(
        "opening", nargs="?", metavar="MESSAGE",
        help="Opening intent handed verbatim to the workflow's opening step "
             "as its first message; omit to start with an interactive prompt. "
             "May also be supplied via --message.")
    p_new.add_argument("--title", metavar="TEXT", default=None,
                       help="Set the quest title explicitly; the workflow's "
                            "opening step sets one if omitted.")
    p_new.add_argument("--headless", action="store_true", dest="non_interactive",
                       help="Run headless: no live prompt; supply the opening "
                            "message via the positional argument or --message.")
    p_new.add_argument("--autonomous", action="store_true",
                       help="Take imp's recommendation at routine decision points "
                            "instead of asking you; questions only you can answer "
                            "still pause for you. Independent of --headless.")
    p_new.add_argument("--message", metavar="TEXT", default=None,
                       help="Alias for the positional opening message (seeds the "
                            "opening step); convenient for headless/remote callers.")
    new_mode = p_new.add_mutually_exclusive_group()
    new_mode.add_argument("--backlog", metavar="TITLE", default=None, dest="idea",
                          help="File an idea for your backlog with this TITLE and "
                               "exit immediately without running (the positional "
                               "MESSAGE is an optional longer brief). Start it later "
                               "with `imp resume <id>`.")
    new_mode.add_argument("--background", action="store_true",
                          help="Create the quest and launch it detached in the "
                               "background, running headless and autonomous; it "
                               "appears in the active list. Implies --headless "
                               "and --autonomous.")
    p_new.add_argument("--depends-on", metavar="ID", action="append",
                       default=None, dest="depends_on",
                       help="A quest id this quest depends on (repeatable). "
                            "Gates go-live until the dep merges; never changes "
                            "the base branch.")
    p_new.add_argument("--workflow", metavar="NAME", default=None,
                       help="Run this quest on workflow NAME instead of the "
                            "project default. A bundled name needs no commit; a "
                            "committed imp/workflows/NAME fork overrides it.")
    p_new.add_argument("--var", metavar="NAME=VALUE", action="append",
                       default=None,
                       help="Seed a workflow variable for this quest only "
                            "(repeatable). NAME must be declared by the workflow; "
                            "VALUE is coerced to its declared type (a bool takes "
                            "true or false).")
    p_new.add_argument("--base", metavar="BRANCH", default=None,
                       help="Base the quest on an existing BRANCH instead of "
                            "the configured default-base. BRANCH must already "
                            "exist.")
    p_new.add_argument("--provider", metavar="NAME", default=None,
                       help="Run this quest on NAME, whatever the configured "
                            "provider order is.")
    p_new.set_defaults(func=_cmd_new, warm=True)

    p_chat = sub.add_parser(
        "chat",
        help="Open a conversation with imp (ask questions, scout work, or "
             "scope a quest).",
        description=(
            "Open a conversation with imp: ask how it works, check on existing "
            "work, or scope a new quest. Nothing is built until you choose to. "
            "Once you've replied, a conversation you leave is archived, still "
            "findable with imp search and resumable with imp resume <id>; if a "
            "quest took shape, it stays in your active list as a draft instead."))
    p_chat.add_argument(
        "opening", nargs="?", metavar="MESSAGE",
        help="Optional opening message; omit to start with an interactive prompt.")
    p_chat.add_argument("--headless", action="store_true", dest="non_interactive",
                        help="Headless: no live prompt; supply the opening message "
                             "via the positional argument or --message.")
    p_chat.add_argument("--autonomous", action="store_true",
                        help="Take imp's recommendation at routine decision points "
                             "instead of asking you; questions only you can answer "
                             "still pause for you. Independent of --headless.")
    p_chat.add_argument("--message", metavar="TEXT", default=None,
                        help="Alias for the positional opening message.")
    p_chat.add_argument("--provider", metavar="NAME", default=None,
                        help="Run this quest on NAME, whatever the configured "
                             "provider order is.")
    p_chat.set_defaults(func=_cmd_chat, warm=True)

    p_resume = sub.add_parser(
        "resume",
        help="Resume a paused or interrupted quest.",
        description=(
            "Resume a paused or interrupted quest, continuing the workflow where "
            "it stopped. With no id on a tty, pick from a list of resumable "
            "quests."))
    p_resume.add_argument(
        "token", nargs="?", metavar="ID",
        help="id of the quest to resume, or any unique part of it.")
    p_resume.add_argument("--headless", action="store_true", dest="non_interactive",
                          help="Headless: no live prompt; supply the reply via --message.")
    p_resume.add_argument("--autonomous", action="store_true",
                          help="Take imp's recommendation at routine decision points "
                               "instead of asking you; questions only you can answer "
                               "still pause for you. Independent of --headless.")
    p_resume.add_argument("--message", metavar="TEXT", default=None,
                          help="Headless reply to the question the quest is waiting "
                               "on (or the next one it asks); answer a later question "
                               "by resuming again with another --message. "
                               "Requires --headless.")
    p_resume.add_argument("--provider", metavar="NAME", default=None,
                          help="Switch the quest to NAME for its remaining "
                               "steps; the current step picks up from the "
                               "event log.")
    p_resume.set_defaults(func=_cmd_resume, warm=True)

    p_log = sub.add_parser(
        "log",
        help="Show the narrative for a quest.",
        description=(
            "Show the narrative for a quest as plain text. --full expands "
            "every fold; --show/--hide toggle individual content categories "
            "(thinking, tools, subagents, prompts, tiers) over the lean view, "
            "in the terminal and in --md/--html exports alike. "
            "Set NO_COLOR=1 to disable colors. "
            "Tip: imp log <id> --full | grep ... for line-oriented scripting."
        ),
    )
    p_log.add_argument(
        "token", nargs="?", metavar="ID",
        help="Quest id, or any unique part of it.")
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
             "grouped by phase, with work time, tokens and cost per step.",
    )
    p_log.add_argument(
        "--no-pager", action="store_true",
        help="Disable the pager even when stdout is a TTY.",
    )
    p_log.add_argument(
        "-f", "--follow", action="store_true",
        help="Live-tail the narrative (tail -f): replay it, then stream new "
             "events until the quest reaches a terminal status. Composes with "
             "--full/--show/--hide; implies --no-pager.",
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
    p_log.set_defaults(func=_cmd_log, parser=p_log)

    p_events = sub.add_parser(
        "events",
        help="Slice and dice a quest's event log (imp events <id>).",
        description=(
            "Index and filter a quest's event log. Bare (imp events <id>): a\n"
            "steps+types index. --step <name>: a scoped index for that step's\n"
            "window. --type / --agent / <range> / --all: compact event lines (#N\n"
            "ordinal, type, [agent], summary). --json for structured output."
        ),
        epilog=(
            "subcommands:\n"
            "  imp events search <query>\n"
            "      Substring search across event content (tool calls, results,\n"
            "      reasoning).\n"
            "      Bounded by default; use --all for exhaustive output. Full flags with:\n"
            "      imp events search -h"
        ),
        formatter_class=ImpRawHelpFormatter,
    )
    p_events.add_argument(
        "token", nargs="?", default=None, metavar="ID",
        help="Quest id, or any unique part of it.")
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
    p_events.set_defaults(func=_cmd_events, parser=p_events)

    p_list = sub.add_parser(
        "list",
        help="List your quests: in-flight, backlog, and completed.",
        description=(
            "With no argument, an overview of your quests — in-flight, the "
            "backlog, and every completed quest. -a/--all also includes "
            "abandoned/archived quests (the block is then labelled `history`)."),
    )
    p_list.add_argument(
        "-a", "--all", action="store_true",
        help="Also include abandoned and archived quests "
             "(the block is then labelled `history`).")
    p_list.add_argument(
        "--active", action="store_true",
        help="Show only in-flight quests and drafts.")
    p_list.add_argument(
        "--backlog", action="store_true",
        help="Show only backlog items.")
    p_list.add_argument(
        "--completed", action="store_true",
        help="Show only completed quests.")
    p_list.add_argument(
        "--abandoned", action="store_true",
        help="Show only abandoned quests.")
    p_list.add_argument(
        "--archived", action="store_true",
        help="Show only archived conversations.")
    p_list.add_argument(
        "--since", metavar="SPEC", default=None,
        help="Show only quests active since SPEC — today, yesterday, this-week, "
             "this-month, or a number + unit h/d/w/m/y (e.g. 2d, 1w).")
    p_list.add_argument(
        "--json", action="store_true",
        help="Emit the quest board as a structured JSON array.")
    p_list.add_argument(
        "--no-pager", action="store_true",
        help="Disable the pager even when stdout is a TTY.")
    p_list.set_defaults(func=_cmd_list)

    p_show = sub.add_parser(
        "show",
        help="Show one quest's detail or one of its saved files (imp show <id>).",
        description=(
            "Print a quest's detail card, which lists its saved files, or name "
            "one to print just that. A name is a file path, or a folder: a folder "
            "holding one file (such as spec, verify or summary) prints that file, "
            "and a folder holding several lists them. Reads the live quest files "
            "while a quest is in flight and the copy saved in git once it is "
            "finished."),
    )
    p_show.add_argument(
        "token", nargs="?", default=None, metavar="ID",
        help="Quest id, or any unique part of it.")
    p_show.add_argument(
        "name", nargs="?", default=None, metavar="NAME",
        help="A saved file or folder to print (default: the detail card).")
    p_show.add_argument(
        "--no-pager", action="store_true",
        help="Disable the pager even when stdout is a TTY.")
    p_show.add_argument(
        "--json", action="store_true",
        help="Emit the overview detail card as a structured JSON object.")
    p_show.set_defaults(func=_cmd_show, parser=p_show)

    p_search = sub.add_parser(
        "search",
        allow_abbrev=False,
        help="Search quests by id, title, or conversation content.",
        description=(
            "Search active (in-flight + drafts), backlog, and history (completed, "
            "abandoned, archived) quests "
            "for the query as a case-insensitive substring of a quest's id, "
            "title, or conversation content. --active, --backlog, --completed, "
            "--abandoned, and --archived narrow the search and combine; with none "
            "of them, everything is searched."),
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
        "--completed", action="store_true",
        help="Search completed quests.")
    p_search.add_argument(
        "--abandoned", action="store_true",
        help="Search abandoned quests.")
    p_search.add_argument(
        "--archived", action="store_true",
        help="Search archived conversations.")
    p_search.add_argument(
        "--merged-after", metavar="COMMIT", default=None,
        help="Keep only completed quests whose merge landed after COMMIT "
             "(implies --completed).")
    p_search.add_argument(
        "-l", "--list", action="store_true", dest="list",
        help="One terse line per quest (id, title, zone); no excerpts.")
    p_search.add_argument(
        "--no-pager", action="store_true",
        help="Disable the pager even when stdout is a TTY.")
    p_search.set_defaults(func=_cmd_search)

    p_config = sub.add_parser(
        "config",
        help="Show the effective config and per-setting provenance; set or "
             "unset a value.",
        description=(
            "Show the effective configuration and where each setting came "
            "from (its provenance layer). --explain TIER traces one tier's "
            "model bindings through the layers; --all shows every "
            "provider x tier row. `imp config set` / `imp config unset` edit "
            "the project or global config file."
        ),
    )
    p_config.add_argument("--explain", metavar="TIER", default=None,
                          help="Trace how one tier's model bindings resolve "
                               "through the configuration layers.")
    p_config.add_argument("--all", action="store_true",
                          help="Show every provider x tier row "
                               "(default: only configured providers).")
    p_config.set_defaults(func=_cmd_config)
    config_sub = p_config.add_subparsers(dest="config_action", required=False)
    p_config_set = config_sub.add_parser(
        "set",
        help="Set a configuration value in the project or global config file.",
        description=(
            "Write one setting to a config file. Without a scope flag, the value "
            "goes to the file that setting normally lives in; --project, --global, "
            "or --local picks the file explicitly. Run 'imp config' to see the "
            "result and where each value comes from."))
    p_config_set.add_argument("key", help=_CONFIG_KEY_HELP)
    p_config_set.add_argument(
        "value", help="New value; list settings take a comma-separated value.")
    _add_config_scope(p_config_set)
    p_config_set.set_defaults(func=_cmd_config_set)
    p_config_unset = config_sub.add_parser(
        "unset",
        help="Comment out a configuration value in the project or global "
             "config file.",
        description=(
            "Comment out one setting in a config file so the next layer down "
            "takes effect. Without a scope flag, it edits the file that setting "
            "normally lives in; --project, --global, or --local picks the file "
            "explicitly."))
    p_config_unset.add_argument("key", help=_CONFIG_KEY_HELP)
    _add_config_scope(p_config_unset)
    p_config_unset.set_defaults(func=_cmd_config_unset)

    p_abandon = sub.add_parser(
        "abandon",
        help="Tear down a quest's worktree, branch, and state (logged).",
        description=(
            "Tear down a built quest — its worktree, branch, and state — "
            "or discard a backlog idea. With no id, pick from a list. "
            "On a terminal, confirm before tearing down a quest that has a "
            "worktree; pass --yes to skip."),
    )
    p_abandon.add_argument(
        "token", nargs="?", metavar="ID",
        help="id of the quest to abandon, or any unique part of it "
             "(omit on a tty to pick from a list).")
    p_abandon.add_argument(
        "--yes", action="store_true",
        help="Skip the confirmation before tearing down a quest that has a worktree.")
    p_abandon.set_defaults(func=_cmd_abandon)

    p_edit = sub.add_parser(
        "edit",
        help=("Edit a quest's title, backlog membership, dependencies, base branch, "
              "or opening intent."),
        description=(
            "Edit a quest's title, backlog membership, dependencies, or base branch, "
            "or append to its opening intent. --backlog/--active, "
            "--depends-on/--clear-deps, and --base work only before a quest is built."))
    p_edit.add_argument("token", metavar="ID", help="Quest id, or any unique part of it.")
    p_edit.add_argument("--title", metavar="TEXT", default=None,
                        help="Set the quest title.")
    p_edit.add_argument(
        "--append-intent", metavar="TEXT", default=None,
        help="Append a verbatim note to an unstarted quest's opening intent.")
    p_edit_zone = p_edit.add_mutually_exclusive_group()
    p_edit_zone.add_argument(
        "--backlog", action="store_true",
        help="Move a quest that isn't built yet to the backlog.")
    p_edit_zone.add_argument(
        "--active", action="store_true",
        help="Return a quest that isn't built yet from the backlog to the active list.")
    p_edit_deps = p_edit.add_mutually_exclusive_group()
    p_edit_deps.add_argument(
        "--depends-on", metavar="ID", action="append", default=None,
        dest="depends_on", help="Replace the dependency list (repeatable).")
    p_edit_deps.add_argument(
        "--clear-deps", action="store_true",
        help="Empty the dependency list.")
    p_edit.add_argument(
        "--base", metavar="BRANCH", default=None,
        help="Re-base a quest on an existing BRANCH. Rejected "
             "once the quest has been built.")
    p_edit.set_defaults(func=_cmd_edit)

    p_rc = sub.add_parser(
        "remote-control",
        help="Run the optional per-repo daemon that relays all quests to one transport.",
        description=(
            "Run the optional per-repo daemon that relays every quest's activity "
            "to a single chat transport (e.g. Telegram) so you can watch and steer "
            "runs remotely. start launches it; stop shuts it down."),
    )
    p_rc.set_defaults(func=_cmd_remote_control, parser=p_rc)
    rc_sub = p_rc.add_subparsers(dest="rc_action", required=False)
    p_rc_start = rc_sub.add_parser(
        "start", help="Start the remote-control daemon for this repo (foreground).",
        description=(
            "Start this repo's remote-control daemon in the foreground; it relays "
            "every quest's activity to the configured chat transport until "
            "stopped."))
    p_rc_start.set_defaults(func=_cmd_remote_control_start)
    p_rc_stop = rc_sub.add_parser(
        "stop", help="Stop the running remote-control daemon (SIGTERM).",
        description=(
            "Stop this repo's running remote-control daemon by sending it "
            "SIGTERM."))
    p_rc_stop.set_defaults(func=_cmd_remote_control_stop)

    p_help = sub.add_parser(
        "help",
        help="Show this help, or one command's options (imp help <command>).",
        description=(
            "Print the top-level command list, or with <command>, that command's "
            "full help (same as 'imp <command> -h')."))
    p_help.add_argument("command", nargs="?", metavar="<command>",
                        help="Command to show help for.")

    return parser


def _command_names(parser: argparse.ArgumentParser) -> set[str]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return set(action.choices)
    return set()


def _top_level_help(parser: argparse.ArgumentParser) -> str:
    # IMP_THEME only: reading config would import the yaml stack into every `imp -h`.
    pal.set_theme(pal.theme_override() or "ansi")
    return parser.format_help()


def _help_command(rest: list[str], parser: argparse.ArgumentParser) -> int:
    if not rest:
        print(_top_level_help(parser), end="")
        return 0
    cmd = "help" if rest[0] in ("-h", "--help") else rest[0]
    if cmd not in _command_names(parser):
        _emit_cli_error(f"unknown command {cmd!r}. Run 'imp help'.")
        return 2
    # `imp help <cmd>` == `imp <cmd> -h`: argparse prints the subparser help and
    # exits 0 (SystemExit), so control never returns past this line.
    parser.parse_args([cmd, "-h"])
    return 0


def _dispatch_meta(args: list[str], parser: argparse.ArgumentParser) -> int | None:
    if not args or args[0] in ("-h", "--help"):
        return _help_command([], parser)
    if args[0] == "help":
        return _help_command(args[1:], parser)
    first = args[0]
    if not first.startswith("-") and first not in _command_names(parser):
        _emit_cli_error(f"unknown command {first!r}. Run 'imp help'.")
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
    from imp.diagnostics import install_stack_dumper
    from imp.engine import worktree
    from imp.engine.errors import LoadError, LoadErrors
    _init_logging()
    args = sys.argv[1:] if argv is None else argv
    parser = _build_parser()
    try:
        events_search = len(args) >= 2 and args[0] == "events" and args[1] == "search"
        if events_search:
            ns = _build_events_search_parser().parse_args(args[2:])
            if error := _events_search_output_control_error(ns):
                _emit_cli_error(error)
                return 2
        with contextlib.suppress(Exception):
            if (claiming := _claiming_settings_root()) is not None:
                install_stack_dumper(
                    project_state_dir(claiming) / f"cli.{os.getpid()}.stack-dump.txt")
        result = _dispatch_meta(args, parser)
        if result is not None:
            # _dispatch_meta may have printed the overview/unknown-command
            # error above; that write belongs inside the guard too. Flush here
            # for the same buffered-pipe reason as the command path below.
            sys.stdout.flush()
            return result
        if not events_search:
            ns = parser.parse_args(args)
        if getattr(ns, "warm", False):
            # Import the graph up front so an on-disk update can't splice into a long run.
            from imp.remote.session.warmup import warm_module_graph
            warm_module_graph()
        _apply_theme()   # resolve dark/light/ansi once, before any command renders
        rc: int = ns.func(ns)
        # Flush here, inside the guard: output small enough to fit Python's
        # stdout buffer is otherwise flushed at interpreter shutdown, where a
        # broken pipe surfaces as an uncatchable "Exception ignored" traceback.
        # Forcing the flush now moves that write where the handler can catch it.
        sys.stdout.flush()
        return rc
    except worktree.PreconditionError as e:
        _emit_cli_error(f"{e}")
        return 2
    except (LoadError, LoadErrors) as e:
        _emit_cli_error(f"{e}")
        return 2
    except BrokenPipeError:
        # A reader (e.g. `imp log | less` quit before draining) closed the pipe.
        # Redirect stdout to devnull so the interpreter's final flush can't
        # re-raise during shutdown, then exit with the conventional SIGPIPE code.
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        return 128 + signal.SIGPIPE
