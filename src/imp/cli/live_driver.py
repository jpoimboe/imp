# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
# src/imp/cli/live_driver.py
"""Live run-view driver: emit-on-finalize folded rendering.

Holds a NarrativeBuilder (the same one imp log's build_tree uses) and, after each
event, emits the nodes that are now final — in tree (DFS) order — through the
folded render_text._render_* helpers. The live view and imp log therefore share
one render path (design Part 2).

Ordering, dedup, and depth come from a driver-owned STACK of frontier cursors —
one per open container the driver is tracking (root -> phase -> step). Each
cursor points at the next unemitted child of its container, advances
left-to-right, emits a child once when final, and blocks on a not-final child.
Depth is read from the cursor stack, NOT the builder's mutable _step_stack /
_current_phase (a scope-closer event pops those BEFORE this post-add() walk runs).

Not-final children (the only things that block a cursor):
  - an open ToolBurstFold (still its owning builder's current_burst; for a
    nested burst the owner is the subagent's nested builder, not the main one).
Everything else is final on append. A SubagentFold is a LEAF (Part D): it
collapses to a single `↳ subagent({name}) · N events ⟨status⟩` status row emitted once the
subagent stops running — in the lean view its streamed children and structured
return value are never rendered (this structurally eliminates the SDK
cancellation-sentinel leak); with the `subagents` category on they expand through
the same render_text path `imp log --full` uses.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from imp.cli import narrative as nv
from imp.cli import palette as pal
from imp.cli import render_text as rt
from imp.cli.live_status import LiveStatus, term_width

if TYPE_CHECKING:
    from imp.cli.accounting import RowUsage
    from imp.providers.quota import QuotaReading

_FG_RESET = "\x1b[22;39m"
_RESET = "\x1b[0m"

_AMBER_AT = 0.75          # of the cutoff
_GAUGE_CELLS = 10

# The events that can move the quest-total dollars; everything else leaves it.
_COST_BEARING = frozenset({"agent_message", "agent_message_usage_patch",
                           "query_result"})

# The two windows both subscription carriers report; anything else renders verbatim.
_WINDOW_WORDS = {"5h": "session", "7d": "week"}


def _paint(text: str, *codes: str, color: bool, over_bg: bool) -> str:
    if not color or not codes:
        return text
    if over_bg:
        return pal.sgr(*codes, enabled=True) + text + _FG_RESET
    return pal.ansi(text, *codes, enabled=True)


def context_bar(provider: str | None, model: str | None,
                prompt_tokens: int | None, *,
                context_pct: float | None = None,
                context_window: int | None = None,
                color: bool, over_bg: bool) -> str | None:
    # Lazy import keeps `import live_driver` free of the providers package.
    from imp.providers.catalog import (
        DEFAULT_CHECKPOINT_FRACTION,
        checkpoint_cutoff,
        limits_for,
    )
    info = limits_for(provider, model)
    # A window the provider reported for this run beats the catalog constant:
    # codex derates its own window per-config, so only the live value is right.
    window = context_window or (info.window if info else None)
    if context_pct is not None:
        frac = context_pct / 100
    else:
        if window is None or prompt_tokens is None:
            return None
        frac = prompt_tokens / window
    # Red begins exactly at the handoff cutoff, so the bar always marks the real
    # point; the live context_pct path has no window to cap against.
    warn = checkpoint_cutoff(info, window)
    if warn is None:
        warn = DEFAULT_CHECKPOINT_FRACTION
    pct = round(frac * 100)
    hue = ("red" if frac >= warn
           else "wordmark" if frac >= _AMBER_AT * warn
           else "green")
    filled = max(0, min(_GAUGE_CELLS, round(frac * _GAUGE_CELLS)))
    gauge = ""
    if filled:
        gauge += _paint("█" * filled, hue, color=color, over_bg=over_bg)
    if filled < _GAUGE_CELLS:
        gauge += _paint("░" * (_GAUGE_CELLS - filled), "gray",
                        color=color, over_bg=over_bg)
    return f"{gauge} {_paint(f'{pct}%', hue, color=color, over_bg=over_bg)}"


def _coarse_left(seconds: float) -> str:
    """Most-significant unit only, nearest, never below a minute: the countdown sits
    beside a percentage, where a second unit of precision reads as noise. The unit is
    promoted when the smaller unit's rounded value saturates (e.g. 60m → 1h)."""
    if round(seconds / 3600) >= 24:
        return f"{round(seconds / 86400)}d"
    if round(seconds / 60) >= 60:
        return f"{round(seconds / 3600)}h"
    return f"{max(1, round(seconds / 60))}m"


def _reset_countdown(resets_at: int | None, now_epoch: float | None) -> str | None:
    """Time until this window resets. None when the carrier stated none or it has
    already passed: the percentage is still true, only the countdown is unknown."""
    if resets_at is None:
        return None
    remaining = resets_at - (time.time() if now_epoch is None else now_epoch)
    if remaining <= 0:
        return None
    return _coarse_left(remaining)


def quota_field(reading: QuotaReading, *, now_epoch: float | None,
                color: bool, over_bg: bool) -> tuple[str, str]:
    """The position row's two right-group candidates, widest first:
    `claude_code · session 62% (3h left) · week 7% (5d left)`, and the same
    windows without the provider segment for a row too narrow to carry it. The
    provider name and the window labels are uncolored and the countdown gray;
    only the percentage is hued, banded on the displayed integer so a number can
    never disagree with its own hue."""
    windows: list[str] = []
    for w in reading.windows:
        pct = pal.quota_pct(w.used_pct)
        label = _WINDOW_WORDS.get(w.label, w.label)
        seg = (label + " "
               + _paint(f"{pct}%", pal.quota_hue(pct), color=color, over_bg=over_bg))
        left = _reset_countdown(w.resets_at, now_epoch)
        if left is not None:
            seg += " " + _paint(f"({left} left)", "gray", color=color, over_bg=over_bg)
        windows.append(seg)
    joined = " · ".join(windows)
    return f"{reading.provider} · {joined}", joined


def cost_field(usage: RowUsage | None) -> str | None:
    """The quest-total dollars, bare: the footer carries no legend, so it carries
    none of the summary cell's sigils either. None when nothing is priced yet."""
    if usage is None or usage.priced == 0:
        return None
    return "<$0.01" if usage.dollars < 0.01 else f"${usage.dollars:.2f}"


def compose_activity_line(*, activity: nv.Activity, frame: int, color: bool,
                          now_iso: str, width: int,
                          pending_provider: str | None = None) -> str:
    """Activity line: spinner + verb + detail + block-age elapsed (always animated).
    Elapsed is the age of the activity's current live block, derived from its
    start_ts against `now_iso` (see nv.activity_elapsed). The retry-hint pair is a
    droppable tail: the widest candidate that survives _draw's clip wins, so a row too
    narrow for both hints sheds hints rather than the countdown."""
    spin = _paint(pal.spinner_frame(frame), "wordmark", color=color, over_bg=False)
    elapsed = nv.activity_elapsed(activity, now_iso)
    head: list[str] = []
    if activity.verb:
        head.append(_paint(activity.verb, "wordmark", color=color, over_bg=False))
    if activity.detail:
        head.append(_paint(activity.detail, "gray", color=color, over_bg=False))
    if activity.countdown_total is not None:
        remaining = max(0.0, activity.countdown_total - elapsed)
        tail = (f"{nv._fmt_hm(remaining)} left" if remaining >= 60
                else f"{rt.fmt_seconds(remaining)} left")
    else:
        tail = rt.fmt_seconds(elapsed)
    rest: list[str] = []
    if activity.dep_wait:
        rest.append(_paint("press ctrl-d to pause", "dim", color=color, over_bg=False))
    if pending_provider is not None:
        rest.append(_paint(f"switching to {pending_provider} after this step",
                           "dim", color=color, over_bg=False))
    rest.append(_paint(tail, "dim", color=color, over_bg=False))
    if activity.countdown_total is not None and activity.countdown_total >= 60:
        ladder: tuple[tuple[str, ...], ...] = (
            ("press r to retry now", "press p to switch provider"),
            ("press r to retry now",),
            (),
        )
    else:
        ladder = ((),)

    def render(hints: tuple[str, ...]) -> str:
        painted = [_paint(h, "dim", color=color, over_bg=False) for h in hints]
        return f"{spin} " + rt.compose_label([*head, *painted, *rest])

    for hints in ladder:
        candidate = render(hints)
        # fits when it survives _draw's clip_display(row, width - 1) byte-identical
        if pal.clip_display(candidate, width - 1) == candidate:
            break
    return candidate


def _band_wrap(body: str, bg: str, width: int) -> str:
    """Pad `body` to width-2 and wrap it in the band bg/reset pair; no-op when bg is empty."""
    if not bg:
        return body
    body_dw = pal.display_width(body)
    target = width - 2
    if body_dw < target:
        body = body + " " * (target - body_dw)
    return f"{bg}{body}{_RESET}"


def _split_pad(left: str, gauged: str, rights: Sequence[str], *,
               width: int) -> str:
    """`gauged` with the widest right-group candidate that still leaves a column
    of gap anchored flush to the band's right edge, degrading right-first: the
    candidates in order, then the right group whole, then the gauge, then clip the
    left group. An empty sequence is no right group."""
    target = width - 2
    gauged_w = pal.display_width(gauged)
    for candidate in rights:
        gap = target - gauged_w - pal.display_width(candidate)
        if gap >= 1:
            return f"{gauged}{' ' * gap}{candidate}"
    if gauged_w <= target:
        return gauged
    if pal.display_width(left) <= target:
        return left
    return pal.clip_display(left, target)


def compose_identity_toolbar(*, quest_id: str | None, title: str, color: bool,
                             width: int, unsandboxed: bool = False,
                             project: str = "") -> str | None:
    """Minimal project · id · title identity bar for the opening prompt, before any
    activity exists. None only when there is nothing to name: a title alone (no
    quest id) still gets a band."""
    bg = pal.toolbar_bg_open(enabled=color)
    over = bool(bg)
    project_seg = (_paint(project, "gray", color=color, over_bg=over)
                   if project else None)
    id_seg = (_paint(quest_id, "wordmark", color=color, over_bg=over)
              if quest_id else None)
    title_seg = title if title else None
    unsandboxed_seg = (_paint("unsandboxed", *pal.NODE_COLORS["error"],
                              color=color, over_bg=over) if unsandboxed else None)
    body = rt.compose_label([project_seg, id_seg, title_seg, unsandboxed_seg])
    if not body:
        return None
    return _band_wrap(body, bg, width)


def compose_position_row(*, activity: nv.Activity, quest_id: str | None,
                         title: str, prompt_tokens: int | None, color: bool,
                         width: int, context_pct: float | None = None,
                         context_window: int | None = None,
                         auth_mode: str | None = None,
                         quota: QuotaReading | None = None,
                         cost: RowUsage | None = None,
                         now_epoch: float | None = None,
                         unsandboxed: bool = False, project: str = "") -> str:
    """Row 3 of the live footer: the left group — project · id · title ·
    unsandboxed · phase · plan · counter · model · context gauge — with the
    quota-or-cost group anchored flush to the band's right edge."""
    # Lazy import keeps `import live_driver` free of the providers package.
    from imp.cli.accounting import is_metered

    bg = pal.toolbar_bg_open(enabled=color)
    over = bool(bg)
    project_seg = (_paint(project, "gray", color=color, over_bg=over)
                   if project else None)
    id_seg = _paint(quest_id, "wordmark", color=color, over_bg=over) if quest_id else None
    title_seg = title if title else None
    unsandboxed_seg = (_paint("unsandboxed", *pal.NODE_COLORS["error"],
                              color=color, over_bg=over) if unsandboxed else None)
    phase_seg = (_paint(activity.phase, *pal.NODE_COLORS["step"], color=color, over_bg=over)
                 if activity.phase else None)
    plan_seg = (_paint(activity.plan, *pal.NODE_COLORS["step"], color=color, over_bg=over)
                if activity.plan else None)
    counter_seg = (_paint(activity.counter, *pal.NODE_COLORS["step"], color=color, over_bg=over)
                   if activity.counter else None)
    model_seg = (_paint(activity.model, "gray", color=color, over_bg=over)
                 if activity.model else None)
    bar = None
    if activity.kind == "agent" and not activity.lane_wait:
        bar = context_bar(activity.provider, activity.model, prompt_tokens,
                          context_pct=context_pct, context_window=context_window,
                          color=color, over_bg=over)
    rights: list[str]
    if is_metered(activity.provider, auth_mode):
        dollars = cost_field(cost)
        rights = [dollars] if dollars else []
    elif quota is not None:
        rights = list(quota_field(quota, now_epoch=now_epoch, color=color,
                                  over_bg=over))
    else:
        rights = []
    left_segs = [project_seg, id_seg, title_seg, unsandboxed_seg,
                 phase_seg, plan_seg, counter_seg, model_seg]
    body = _split_pad(rt.compose_label(left_segs),
                      rt.compose_label([*left_segs, bar]), rights, width=width)
    return _band_wrap(body, bg, width)


def compose_prelaunch_position_row(
    *, phase: str | None, plan: str | None, step_label: str | None,
    model: str | None, quest_id: str | None, title: str,
    color: bool, width: int, unsandboxed: bool = False, project: str = "",
) -> str:
    """Pre-launch position row for the inert-resume prompt (no context-usage bar)."""
    activity = nv.Activity(
        phase=phase, plan=plan, counter=step_label, model=model,
        kind=None, lane_wait=False)
    return compose_position_row(
        activity=activity, quest_id=quest_id, title=title, project=project,
        prompt_tokens=None, color=color, width=width, unsandboxed=unsandboxed)


def compose_subagent_rows(builder: nv.NarrativeBuilder, *, color: bool,
                          width: int, now_iso: str) -> list[str]:
    """One row per running subagent (B4/AC5); colliding roles numbered.
    Each row carries the parent position row's band background, padded to width-2
    so the band spans the full footer width and aligns with the row above it."""
    folds = builder.running_subagents()
    if not folds:
        return []
    counts: dict[str, int] = {}
    for f in folds:
        counts[f.subagent_name] = counts.get(f.subagent_name, 0) + 1
    seen: dict[str, int] = {}
    rows: list[str] = []
    bg = pal.toolbar_bg_open(enabled=color)
    over = bool(bg)
    for f in folds:
        name = f.subagent_name
        if counts[name] > 1:
            seen[name] = seen.get(name, 0) + 1
            role = f"{name} {seen[name]}"
        else:
            role = name
        role_seg = _paint(role, *pal.NODE_COLORS["subagent"], color=color, over_bg=over)
        model = rt.subagent_model(f)
        provider = rt.subagent_provider(f)
        model_seg = _paint(model, "gray", color=color, over_bg=over) if model else None
        bar = context_bar(provider, model, f.nested.latest_prompt_tokens,
                          context_pct=f.nested.latest_context_pct,
                          context_window=f.nested.latest_context_window,
                          color=color, over_bg=over)
        secs = nv.elapsed_seconds(f.start_ts, now_iso)
        if secs is not None:
            secs = max(0.0, secs - f.nested.backoff_wait_seconds(now_iso))
        el = rt.fmt_seconds(secs) if secs is not None else None
        el_seg = _paint(el, "dim", color=color, over_bg=over) if el else None
        arrow = _paint("↳", *pal.NODE_COLORS["subagent"], color=color, over_bg=over)
        inner = arrow + " " + rt.compose_label(
            [role_seg, el_seg, model_seg, bar])
        if bg:
            pad = width - 2 - pal.display_width(inner)
            if pad > 0:
                inner += " " * pad
            inner = f"{bg}{inner}{_RESET}"
        rows.append(inner)
    return rows


@dataclass
class _Cursor:
    container: Any   # WorkflowNode | PhaseNode | StepNode | SubagentFold
    depth: int       # depth at which this container's CHILDREN render
    index: int = 0   # next unemitted child
    emit_mark: int = 0  # driver emission count when this container opened


def _is_container(node: Any) -> bool:
    # A deferred-leaf StepNode is NOT a streamed container: it has no narrative
    # children and renders as a single deferred leaf (its routing arrow, or
    # nothing), mirroring render_text's deferred_leaf branch so live == imp log.
    if isinstance(node, nv.StepNode):
        return not rt.deferred_leaf(node)
    return isinstance(node, nv.PhaseNode)


def _container_children(container: Any) -> list[Any]:
    children: list[Any] = container.children
    return children


def _container_closed(container: Any) -> bool:
    return getattr(container, "closed", False)


class LiveDriver:
    def __init__(self, *, stream: Any, color: bool,
                 width: int | Callable[[], int], status: LiveStatus,
                 display_width: int | Callable[[], int] = 80,
                 suppress_replay: bool = False,
                 preceded_by_blank: bool = False,
                 startup_verb: str | None = None,
                 quest_id: str | None = None, title: str = "",
                 project: str = "",
                 cats: frozenset[str] = frozenset()) -> None:
        self._builder = nv.NarrativeBuilder()
        if quest_id is not None or title:
            self._builder.seed_identity(quest_id, title)
        self._stream = stream
        self._color = color
        self._project = project
        # Content categories (render_text.LOG_CATEGORIES). Empty is the lean view:
        # _resolve_cats returns a non-None set unchanged, so it never means "full".
        self._cats = cats
        # width/display_width may be a live callable (a real terminal, re-read
        # per emission so markdown reflows after a resize) or a fixed int (piped
        # capture, tests). The status footer already polls term_width() each tick;
        # resolving here keeps the permanent blocks in step with it.
        self._width = width
        self._display_width = display_width
        self._status = status
        self._suppressing = suppress_replay
        # The footer is decoupled from scrollback suppression: it lights at the
        # replay_complete boundary (see on_event) even though _suppressing stays
        # True until the first live continuation node. Starts True for the non-
        # suppressing paths (imp new/chat/log --follow) so their footer is live
        # from the first event with no boundary.
        self._footer_live = not suppress_replay
        # Snapshot of every tree-node id present at the replay_complete boundary
        # — i.e. the whole replayed prefix. The walk lifts suppression only when
        # it reaches a node NOT in this set (a live continuation node), so a prior
        # phase's completion breadcrumb (emitted when a live event closes the
        # replayed phase) stays suppressed. None until the boundary arrives.
        self._replayed_node_ids: set[int] | None = None
        # True after the last emitted block was agent prose, so the next prose
        # block gets one blank-line separator — matching render_text's lean spacing
        # so the live view and `imp log` look identical (design §5.4). Any non-prose
        # output resets it; an empty emission leaves it untouched.
        self._prev_prose = False
        self._prompt_active = False        # defer _print_lines while a prompt owns the screen
        self._separator_emitted = False    # prepare_for_prompt printed the leading blank
        self._preceded_by_blank = preceded_by_blank
        self._startup_verb = startup_verb
        self._startup_start_iso: str | None = None   # block start for the startup block-age timer
        self._unsandboxed = False
        self._pending_provider: str | None = None
        self._emitted_any = False
        self._emissions = 0
        self._last_header_position: str | None = None
        self._deferred: list[tuple[list[str], bool, str]] = []
        # Every event this driver consumed, and the accounting total derived from
        # them by the summary's own code — one computation, so the live figure and
        # `imp log -s` cannot disagree. Each driver sees each event exactly once.
        self._events: list[dict[str, Any]] = []
        self._cost: RowUsage | None = None
        self._cost_stale = False
        # root cursor: render root's children at depth 0 (matches render_tree).
        self._frontier: list[_Cursor] = [
            _Cursor(container=self._builder.tree.root, depth=0)]

    # -- engine callbacks -----------------------------------------------------
    def on_event(self, event: Any) -> None:
        if isinstance(event, dict) and event.get("type") == "replay_complete":
            # End-of-replay boundary: a transport-level marker the session daemon
            # appends to every replay snapshot (never persisted, never a narrative
            # event). It is the SOLE freeze/light point. Freeze the replayed tree —
            # every node that exists now is replayed and stays suppressed, including
            # a prior phase's completion breadcrumb — so the walk lifts scrollback
            # suppression only at a node created after this point (see _maybe_lift).
            # Light the footer NOW so a resume into a live step that emits no fresh
            # event (a merge-queue wait) still paints its working footer. Return
            # before _builder.add: the payload-less marker would otherwise trip the
            # builder's malformed-payload ErrorNode.
            self._replayed_node_ids = self._snapshot_node_ids()
            self._footer_live = True
            self._refresh_status()
            return
        if isinstance(event, dict):
            self._events.append(event)
            if event.get("type") in _COST_BEARING:
                self._cost_stale = True
        self._builder.add(event)
        self._walk()
        self._maybe_reemit_header()
        self._refresh_status()

    def seed_pending_reply(self, text: str) -> None:
        # Optimistic local echo for a terminal your-turn reply. Emission flows
        # through _walk/_maybe_lift (never a direct write) so replay suppression
        # lifts at the brand-new pending node before it emits.
        self._builder.seed_pending_reply(text)
        self._walk()
        self._refresh_status()

    def echo_submitted_reply(self, text: str) -> None:
        # Force this node past suppress_replay now; survives replay, reconciles the live turn.
        self._builder.seed_pending_reply(text)
        was_suppressing, self._suppressing = self._suppressing, False
        try:
            self._walk()
        finally:
            self._suppressing = was_suppressing
        self._refresh_status()

    def append_committed_reply(self, text: str) -> None:
        # Committed, non-reconciling echo for a control slash command (no daemon
        # user_message will arrive). Mirrors seed_pending_reply's walk/refresh but
        # arms no _pending_reply_node.
        self._builder.append_committed_reply(text)
        self._walk()
        self._refresh_status()

    @property
    def unsandboxed(self) -> bool:
        return self._unsandboxed

    def set_unsandboxed(self, value: bool) -> None:
        self._unsandboxed = value
        self._refresh_status()

    def set_pending_provider(self, name: str | None) -> None:
        self._pending_provider = name
        self._refresh_status()

    def emit_lines(self, lines: list[str]) -> None:
        """Commit plain lines straight to scrollback, outside the narrative tree."""
        self._print_lines(lines)

    def retry_countdown_in_flight(self) -> bool:
        """True while the activity carries a retry countdown — the window in which
        `r` is honored, and the only one in which the `p` picker opens."""
        activity = self._builder.current_activity()
        return activity is not None and activity.countdown_total is not None

    def seed_opening_intent(self, text: str) -> None:
        # Optimistic local echo at submit time; mirrors seed_pending_reply (emits via _walk).
        self._builder.seed_opening_intent(text)
        # Non-TUI only: arm the pre-boot footer spinner (superseded once a step starts).
        self._builder.seed_first_spinner()
        self._walk()
        self._refresh_status()

    def finish(self, *, exit_block: bool = True) -> None:
        """Flush the block; exit_block descends past the owned blank (end_run) else resets."""
        self._status.clear_stop_ack()      # the ack never outlives the run
        self._builder.finalize()
        self._walk(force=True)
        if exit_block:
            self._status.end_run()
        else:
            self._status.reset()

    def compose_status(self, frame: int) -> list[str] | None:
        activity = self._builder.current_activity()
        if activity is None:
            return None
        now_iso = datetime.now(UTC).isoformat()
        w = term_width()
        a_line = compose_activity_line(
            activity=activity, frame=frame, color=self._color, now_iso=now_iso,
            width=w, pending_provider=self._pending_provider)
        root = self._builder.tree.root
        p_row = compose_position_row(
            activity=activity, quest_id=root.id, title=root.title,
            project=self._project,
            prompt_tokens=self._builder.latest_prompt_tokens,
            color=self._color, width=w,
            context_pct=self._builder.latest_context_pct,
            context_window=self._builder.latest_context_window,
            auth_mode=self._builder.auth_mode_for(activity.provider),
            quota=self._builder.quota_for(activity.provider),
            cost=self._quest_cost(),
            unsandboxed=self._unsandboxed)
        rows = [a_line, "", p_row]
        rows += compose_subagent_rows(
            self._builder, color=self._color, width=w, now_iso=now_iso)
        return rows

    def compose_startup(self, frame: int) -> list[str] | None:
        if self._startup_verb is None:
            return None
        now_iso = datetime.now(UTC).isoformat()
        a_line = compose_activity_line(
            activity=nv.Activity(verb=self._startup_verb,
                                 start_ts=self._startup_start_iso),
            frame=frame, color=self._color, now_iso=now_iso, width=term_width())
        root = self._builder.tree.root
        identity_row = compose_identity_toolbar(
            quest_id=root.id, title=root.title, project=self._project,
            color=self._color, width=term_width(), unsandboxed=self._unsandboxed)
        if identity_row is None:
            return [a_line]
        return [a_line, "", identity_row]

    def install_startup_status(self) -> None:
        if self._startup_verb is None:
            return
        self._startup_start_iso = datetime.now(UTC).isoformat()
        if self._preceded_by_blank:
            self._status.skip_leading_blank()
        self._status.update(self.compose_startup)

    def compose_toolbar_static(self) -> str | None:
        # The flat position row under the interactive prompt (no spinner/elapsed).
        # Before any activity, fall back to a minimal id · title identity bar.
        activity = self._builder.current_activity()
        root = self._builder.tree.root
        if activity is None:
            return compose_identity_toolbar(
                quest_id=root.id, title=root.title, project=self._project,
                color=self._color, width=term_width(), unsandboxed=self._unsandboxed)
        return compose_position_row(
            activity=activity, quest_id=root.id, title=root.title,
            project=self._project,
            prompt_tokens=self._builder.latest_prompt_tokens,
            color=self._color, width=term_width(),
            context_pct=self._builder.latest_context_pct,
            context_window=self._builder.latest_context_window,
            auth_mode=self._builder.auth_mode_for(activity.provider),
            quota=self._builder.quota_for(activity.provider),
            cost=self._quest_cost(),
            unsandboxed=self._unsandboxed)

    def _quest_cost(self) -> RowUsage | None:
        """The quest total, by the summary's own computation over the retained
        events — evaluated per drawn frame, not per event."""
        if self._cost_stale:
            # Lazily imported, like context_bar's catalog import.
            from imp.cli.render_summary import build_summary, summary_totals
            _work, self._cost = summary_totals(
                build_summary(self._events), None)
            self._cost_stale = False
        return self._cost

    # -- emission -------------------------------------------------------------
    def _cur_width(self) -> int:
        w = self._width
        return w() if callable(w) else w

    def _cur_display_width(self) -> int:
        dw = self._display_width
        return dw() if callable(dw) else dw

    def _walk(self, *, force: bool = False) -> None:
        while self._frontier:
            cur = self._frontier[-1]
            children = _container_children(cur.container)
            if cur.index >= len(children):
                if len(self._frontier) == 1:
                    return                      # root: never pop; await more children
                if force or _container_closed(cur.container):
                    if (isinstance(cur.container, nv.PhaseNode)
                          and _container_closed(cur.container)
                          and nv.phase_elapsed(cur.container) is not None
                          and not rt.frame_suppressed(cur.container)):
                        # The phase-complete line is surrounded by blanks: arm
                        # prev_prose for the blank ABOVE, and pass prose=True so the
                        # next phase header gets the blank BELOW (matching the batch
                        # walk's spaces_before/after(PhaseNode)).
                        self._prev_prose = True
                        self._print_lines(
                            [rt.phase_breadcrumb_line(
                                cur.container, depth=cur.depth, color=self._color,
                                gutters=self._gutters())],
                            prose=True,
                            separator_indent=rt._gutter_indent(
                                cur.depth, self._gutters(), self._color))
                    if (isinstance(cur.container, nv.StepNode)
                            and self._cats >= rt._ALL_CATEGORIES
                            and cur.container.agent_result):
                        out_lines = list(rt._render_output_args(
                            cur.container.agent_result, depth=cur.depth,
                            color=self._color, width=self._cur_width(),
                            gutters=self._gutters()))
                        if out_lines:
                            # The batch walk yields this block flush against the
                            # step's own last line, then spaces the step as a whole.
                            # A step that emitted nothing has no line to sit flush
                            # against, so it keeps the separator its predecessor armed.
                            if self._emissions > cur.emit_mark:
                                self._prev_prose = False
                            self._print_lines(
                                out_lines, prose=True,
                                separator_indent=rt._gutter_indent(
                                    cur.depth, self._gutters(), self._color))
                    self._frontier.pop()        # container drained + closed
                    continue
                return                          # open container, no new children yet
            child = children[cur.index]
            if not force and not self._is_final(child, container=cur.container):
                return                          # block on a not-final child
            cur.index += 1
            self._maybe_lift(child)
            if _is_container(child):
                # PhaseNode/StepNode: print the header now and descend, streaming
                # the container's children as their events route in. The transcript
                # is flat: neither a phase nor a step adds depth, so prose aligns
                # under the header instead of burying it — must match
                # render_text._render. (aligned indent)
                gutters = self._gutters()
                mark = self._emissions      # before the header: it is this step's own output
                suppress = rt.frame_suppressed(child)
                if not suppress and rt.spaces_before(child):
                    self._prev_prose = True
                if not suppress:
                    self._stream_or_hold(
                        rt.header_line(child, depth=cur.depth, color=self._color,
                                       gutters=gutters),
                        depth=cur.depth, gutters=gutters)
                    if isinstance(child, nv.StepNode):
                        entry_act = self._builder.current_activity()
                        self._last_header_position = (
                            entry_act.position if entry_act is not None else None)
                if not suppress and isinstance(child, (nv.StepNode, nv.PhaseNode)):
                    self._prev_prose = True
                child_depth = cur.depth
                self._frontier.append(
                    _Cursor(container=child, depth=child_depth, emit_mark=mark))
            else:
                if not (isinstance(child, nv.BreadcrumbLine)
                        and rt.frame_suppressed(cur.container)):
                    self._emit_leaf(child, cur.depth, gutters=self._gutters())

    def _snapshot_node_ids(self) -> set[int]:
        acc: set[int] = set()
        stack = [self._builder.tree.root]
        while stack:
            node = stack.pop()
            acc.add(id(node))
            stack.extend(getattr(node, "children", ()))
        return acc

    def _maybe_lift(self, node: Any) -> None:
        # Lift suppression the moment the walk reaches a node created after the
        # replay boundary (a live continuation node). Replayed nodes — and the
        # prior phase's breadcrumb, emitted at the pop site while still
        # suppressed — are left hidden.
        if (self._suppressing and self._replayed_node_ids is not None
                and id(node) not in self._replayed_node_ids):
            self._suppressing = False
            # _emitted_any is True only from the boot echo — preserve its armed blank.
            self._prev_prose = self._emitted_any

    def _is_final(self, node: Any, *, container: Any) -> bool:
        if isinstance(node, nv.StepNode) and rt.deferred_leaf(node):
            return node.closed
        if isinstance(node, nv.SubagentFold):
            # A subagent is a LEAF (Part D): final once it stops running, so the
            # status row resolves to its terminal status on close. While running it
            # blocks the cursor (its row is shown only resolved); the running cue
            # lives in the status line via current_activity.
            return not node.running
        if isinstance(node, nv.ToolBurstFold):
            # A burst is "open" (not yet final) only against the current_burst of
            # the builder that OWNS it. With subagents now leaves, only top-level
            # bursts reach the live walk, so the owning builder is always the main
            # one — no reliance on the A2 drop.
            return node is not self._builder.current_burst
        # Tagged provider-error prose is withheld until turn resolves; `held` drives `imp log` only.
        return not (isinstance(node, nv.AgentTextNode) and node.error_pending)

    def _gutters(self) -> tuple[int, ...]:
        """Subagent boundary columns for the shared render helpers.

        Part D made SubagentFold a leaf: it is emitted via _emit_leaf and never
        pushed onto the frontier as a cursor. Only PhaseNode/StepNode containers
        are pushed (_is_container). Therefore there are no SubagentFold cursors
        in self._frontier during the live walk and this always returns (). The
        magenta │ gutter is added internally by render_text's expanded subagent
        fold, never by this indent."""
        return ()

    def _emit_leaf(self, node: Any, depth: int,
                   gutters: tuple[int, ...] = ()) -> None:
        if isinstance(node, (nv.MergedNode, nv.CompletedNode)):
            # Only the node's spaces_before blank is ours (main.py owns the line): hide()
            # climbs onto the footer's blank so this replaces it; a set _prev_prose doubles it.
            self._prev_prose = False
            self._print_lines([""])
            return
        if isinstance(node, nv.UserMessageNode):
            # The interactive prompt uses erase_when_done, so prompt_toolkit leaves
            # NO echo in scroll-back on a real terminal — we reprint the turn here
            # (no longer suppressed on a TTY). Rendered as a user turn: flush-left,
            # marked by the bright-blue `> ` caret,
            # spaced like a chat message. The same path serves a non-TTY sink
            # (offline capture / `imp run | tee` / the snap-ui mockup), so live and
            # `imp log` show the user's turn identically.
            lines = rt._render(node, depth=depth, cats=self._cats, color=self._color,
                               width=self._cur_width(),
                               display_width=self._cur_display_width(),
                               gutters=gutters)
            self._print_lines(
                list(lines), prose=True,
                separator_indent=rt._gutter_indent(depth, gutters, self._color))
            return
        leaf_lines = list(rt._render(node, depth=depth, cats=self._cats, color=self._color,
                                     width=self._cur_width(),
                                     display_width=self._cur_display_width(),
                                     gutters=gutters))
        # A node that renders nothing must not arm the separator (mirrors _render_children).
        if leaf_lines and rt.spaces_before(node):
            self._prev_prose = True
        prose = rt.spaces_after(node, full=self._cats >= rt._ALL_CATEGORIES)
        self._print_lines(
            leaf_lines, prose=prose,
            separator_indent=rt._gutter_indent(depth, gutters, self._color))

    def _stream_or_hold(self, line: str, *, depth: int,
                        gutters: tuple[int, ...]) -> None:
        self._print_lines(
            [line],
            separator_indent=rt._gutter_indent(depth, gutters, self._color))

    def _print_lines(self, lines: list[str], *, prose: bool = False,
                     separator_indent: str = "") -> None:
        # The ONE output chokepoint, so prose spacing stays consistent: a blank
        # line precedes ANY emission whose immediate predecessor was a prose
        # leaf. `separator_indent` is the gutter-bearing indent at the next
        # emission's depth, so the magenta │ stays continuous inside a
        # SubagentFold (mirroring render_text._render_children). An empty
        # emission returns early and leaves _prev_prose untouched (so a lean-
        # mode thinking/tool turn between two prose blocks doesn't break their
        # pairing). While a prompt is active, non-empty emissions are deferred
        # to flush_deferred() so the prompt screen stays clean (§5a).
        if self._suppressing:
            return
        if not lines:
            return
        self._emissions += 1
        if self._prompt_active:
            self._deferred.append((lines, prose, separator_indent))
            return
        self._emit_now(lines, prose=prose, separator_indent=separator_indent)

    def _emit_now(self, lines: list[str], *, prose: bool, separator_indent: str) -> None:
        if not lines:
            return
        self._status.hide()
        if self._separator_emitted:
            # prepare_for_prompt already printed the leading blank; consume it
            # (only a non-empty emission consumes — this method is non-empty).
            self._separator_emitted = False
        elif self._prev_prose and not (self._preceded_by_blank and not self._emitted_any):
            self._stream.write(separator_indent + "\n")
        self._emitted_any = True
        for ln in lines:
            self._stream.write(ln + "\n")
        self._stream.flush()
        self._prev_prose = prose

    def _frontier_depth(self) -> int:
        """Depth at which the open frontier cursor renders its children — the depth
        of a synthesized user turn (and its separator) in imp log. Empty frontier
        (malformed stream) → 0 (a bare \\n, indent-free)."""
        return self._frontier[-1].depth if self._frontier else 0

    def prompt_indent(self) -> str:
        """Indent string aligning the solicit caret with the open step's content column."""
        return rt._gutter_indent(
            self._frontier_depth(), self._gutters(), self._color)

    def prepare_for_prompt(self) -> None:
        """Flush the armed separator before the interactive prompt draws, record
        that it was emitted (consumed by the next non-empty _print_lines), and mark
        the prompt active so subsequent _print_lines are deferred (§5a). Self-hiding
        and idempotent."""
        self._status.hide()
        if self._prev_prose:
            depth = self._frontier_depth()
            indent = rt._gutter_indent(depth, self._gutters(), self._color)
            self._stream.write(indent + "\n")
            self._stream.flush()
            self._prev_prose = False
            self._separator_emitted = True
        self._prompt_active = True

    def flush_deferred(self) -> None:
        """Clear the prompt-active flag and flush any blocks deferred during the
        prompt (§5a). Called from the solicit `finally` (submit / abandon / EOF) so
        the run is never left muted."""
        self._prompt_active = False
        deferred, self._deferred = self._deferred, []
        for lines, prose, sep in deferred:
            self._emit_now(lines, prose=prose, separator_indent=sep)

    def show_stop_ack(self, text: str) -> None:
        self._status.show_stop_ack(text)

    def _maybe_reemit_header(self) -> None:
        # On a mid-step POSITION change, emit a fresh position-only header into the
        # scrollback (deduped). A verb push or the internal phrase rotation does not
        # move the position, so it never re-emits.
        if not self._builder._step_stack:
            return
        activity = self._builder.current_activity()
        position = activity.position if activity is not None else None
        if position is None or position == self._last_header_position:
            return
        top = self._builder._step_stack[-1]
        if rt.frame_suppressed(top) or rt.deferred_leaf(top):
            return
        self._prev_prose = True
        self._print_lines(
            [rt.header_line(top, depth=self._frontier_depth(), color=self._color,
                            gutters=self._gutters())],
            prose=True,
            separator_indent=rt._gutter_indent(
                self._frontier_depth(), self._gutters(), self._color))
        self._last_header_position = position

    def _refresh_status(self) -> None:
        # The status line is TRANSIENT: it draws on the line just below the last
        # permanent output (a bare \r-rewritten line, no newline) and is erased in
        # place by the next _print_lines (status.hide) before that block prints,
        # so the block overwrites it. We deliberately do NOT inject a "\n" to
        # "float" it below a blank — that newline committed the spinner line into
        # scroll-back (a stray `name · working` above each breadcrumb) AND stacked
        # with _print_lines' own separator to make double blanks. Permanent inter-
        # block spacing is owned entirely by _print_lines; the status never adds to
        # it. `_prev_prose` is left untouched so the next block still gets its one
        # separator blank (which lands on the row the status was using).
        if not self._footer_live:
            # The footer stays dark until the replay_complete boundary lights it
            # (on_event sets _footer_live). Decoupled from _suppressing: the footer
            # can wake at the boundary while the replayed scrollback stays dark.
            # Return early without swapping the compose callback, so whatever is
            # currently installed persists through the replayed prefix: nothing (footer
            # dark, normal start) or compose_startup (startup spinner, when a resume
            # driver called install_startup_status). compose_status replaces it once
            # the boundary lights the footer.
            return
        if self._builder.current_activity() is not None:
            self._status.update(self.compose_status)
        else:
            self._status.reset()
