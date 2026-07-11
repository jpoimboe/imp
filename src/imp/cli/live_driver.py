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
subagent stops running — its streamed children and structured return value are
never rendered in the live/lean views (this structurally eliminates the SDK
cancellation-sentinel leak).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from imp.cli import narrative as nv
from imp.cli import palette as pal
from imp.cli import render_text as rt
from imp.cli.live_status import LiveStatus, term_width

_FG_RESET = "\x1b[22;39m"
_RESET = "\x1b[0m"

_CONTEXT_WIDTH = 10
_BAR_GREEN_MAX = 0.60
_BAR_YELLOW_MAX = 0.85


def _paint(text: str, *codes: str, color: bool, over_bg: bool) -> str:
    if not color or not codes:
        return text
    if over_bg:
        return pal.sgr(*codes, enabled=True) + text + _FG_RESET
    return pal.ansi(text, *codes, enabled=True)


def context_bar(model: str | None, prompt_tokens: int | None, *,
                context_pct: float | None = None,
                checkpoint_threshold: float | int | None = None,
                color: bool, over_bg: bool) -> str | None:
    # Lazy import keeps `import live_driver` free of the providers package; the
    # catalog is the renderer's only window source.
    from imp.providers.catalog import checkpoint_fraction, window_for_model
    window = window_for_model(model)
    if context_pct is not None:
        frac = context_pct / 100
    else:
        if window is None or prompt_tokens is None:
            return None
        frac = prompt_tokens / window
    # The warn (red) cutoff tracks the configured checkpoint threshold when one is
    # known; otherwise the static yellow->red band applies. `window` here is the
    # static catalog window — an absolute-token threshold is resolved against it
    # even on the live `context_pct` path (a slight mismatch if the windows differ).
    warn = checkpoint_fraction(checkpoint_threshold, window)
    if warn is None:
        warn = _BAR_YELLOW_MAX
    filled = max(0, min(_CONTEXT_WIDTH, round(frac * _CONTEXT_WIDTH)))
    pct = round(frac * 100)
    hue = ("green" if frac < _BAR_GREEN_MAX
           else "wordmark" if frac < warn else "red")
    fill = _paint("█" * filled, hue, color=color, over_bg=over_bg) if filled else ""
    track = (_paint("░" * (_CONTEXT_WIDTH - filled), "gray", color=color, over_bg=over_bg)
             if filled < _CONTEXT_WIDTH else "")
    return f"{fill}{track} {pct}%"


def compose_activity_line(*, activity: nv.Activity, frame: int, color: bool,
                          now_iso: str) -> str:
    """Activity line: spinner + verb + detail + block-age elapsed (always animated).
    Elapsed is the age of the activity's current live block, derived from its
    start_ts against `now_iso` (see nv.activity_elapsed)."""
    spin = _paint(pal.spinner_frame(frame), "wordmark", color=color, over_bg=False)
    elapsed = nv.activity_elapsed(activity, now_iso)
    parts: list[str | None] = []
    if activity.verb:
        parts.append(_paint(activity.verb, "wordmark", color=color, over_bg=False))
    if activity.detail:
        parts.append(_paint(activity.detail, "gray", color=color, over_bg=False))
    if activity.countdown_total is not None:
        remaining = max(0.0, activity.countdown_total - elapsed)
        tail = (f"{nv._fmt_hm(remaining)} left" if remaining >= 60
                else f"{rt.fmt_seconds(remaining)} left")
    else:
        tail = rt.fmt_seconds(elapsed)
    parts.append(_paint(tail, "dim", color=color, over_bg=False))
    return f"{spin} " + rt.compose_label([p for p in parts if p])


def compose_position_bar(*, activity: nv.Activity, quest_id: str | None,
                         title: str, prompt_tokens: int | None, color: bool,
                         width: int, context_pct: float | None = None,
                         checkpoint_threshold: float | int | None = None,
                         pad_slate: bool = True) -> str:
    """Position bar: id · title · position · model context-bar (no spinner, no elapsed)."""
    bg = pal.user_bg_open(enabled=color)
    over = bool(bg)
    id_seg = _paint(quest_id, "wordmark", color=color, over_bg=over) if quest_id else None
    title_seg = title if title else None
    position_seg = (_paint(activity.position, "blue", color=color, over_bg=over)
                    if activity.position else None)
    model_seg = (_paint(activity.model, "magenta", color=color, over_bg=over)
                 if activity.model else None)
    bar = context_bar(activity.model, prompt_tokens, context_pct=context_pct,
                      checkpoint_threshold=checkpoint_threshold,
                      color=color, over_bg=over)
    body = rt.compose_label([id_seg, title_seg, position_seg,
                             rt.join_space(model_seg, bar)])
    if pad_slate and bg:
        body_dw = pal.display_width(body)
        target = width - 2
        if body_dw < target:
            body = body + " " * (target - body_dw)
    return f"{bg}{body}{_RESET}" if bg else body


def _slate_wrap(body: str, bg: str, width: int) -> str:
    """Pad `body` to width-2 and wrap it in the slate bg/reset pair; no-op when bg is empty."""
    if not bg:
        return body
    body_dw = pal.display_width(body)
    target = width - 2
    if body_dw < target:
        body = body + " " * (target - body_dw)
    return f"{bg}{body}{_RESET}"


def compose_identity_toolbar(*, quest_id: str | None, title: str, color: bool,
                             width: int) -> str | None:
    """Minimal id · title identity bar for the opening prompt, before any activity exists."""
    if not quest_id:
        return None
    bg = pal.user_bg_open(enabled=color)
    over = bool(bg)
    id_seg = _paint(quest_id, "wordmark", color=color, over_bg=over)
    title_seg = title if title else None
    body = rt.compose_label([id_seg, title_seg])
    return _slate_wrap(body, bg, width)


def compose_position_row(*, activity: nv.Activity, quest_id: str | None,
                         title: str, prompt_tokens: int | None, color: bool,
                         width: int, context_pct: float | None = None,
                         checkpoint_threshold: float | int | None = None) -> str:
    """Row 3 of the live footer: one flat, left-aligned line ordered
    id · title · phase · plan · counter · model context-bar."""
    bg = pal.user_bg_open(enabled=color)
    over = bool(bg)
    id_seg = _paint(quest_id, "wordmark", color=color, over_bg=over) if quest_id else None
    title_seg = title if title else None
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
        bar = context_bar(activity.model, prompt_tokens, context_pct=context_pct,
                          checkpoint_threshold=checkpoint_threshold,
                          color=color, over_bg=over)
    body = rt.compose_label([id_seg, title_seg, phase_seg, plan_seg, counter_seg,
                             rt.join_space(model_seg, bar)])
    return _slate_wrap(body, bg, width)


def compose_subagent_rows(builder: nv.NarrativeBuilder, *, color: bool,
                          width: int, now_iso: str) -> list[str]:
    """One row per running subagent (B4/AC5); colliding roles numbered.
    Each row carries the parent position row's slate background, padded to width-2
    so the slate spans the full footer width and aligns with the row above it."""
    folds = builder.running_subagents()
    if not folds:
        return []
    counts: dict[str, int] = {}
    for f in folds:
        counts[f.subagent_name] = counts.get(f.subagent_name, 0) + 1
    seen: dict[str, int] = {}
    rows: list[str] = []
    bg = pal.user_bg_open(enabled=color)
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
        model_seg = _paint(model, "gray", color=color, over_bg=over) if model else None
        bar = context_bar(model, f.nested.latest_prompt_tokens,
                          context_pct=f.nested.latest_context_pct,
                          color=color, over_bg=over)
        secs = nv.elapsed_seconds(f.start_ts, now_iso)
        if secs is not None:
            secs = max(0.0, secs - f.nested.backoff_wait_seconds(now_iso))
        el = rt.fmt_seconds(secs) if secs is not None else None
        el_seg = _paint(el, "dim", color=color, over_bg=over) if el else None
        arrow = _paint("↳", *pal.NODE_COLORS["subagent"], color=color, over_bg=over)
        inner = arrow + " " + rt.compose_label(
            [role_seg, el_seg, rt.join_space(model_seg, bar)])
        if bg:
            pad = width - 2 - pal.display_width(inner)
            if pad > 0:
                inner += " " * pad
            inner = f"{bg}{inner}{_RESET}"
        rows.append(inner)
    return rows


def compose_activity_status(
    builder: nv.NarrativeBuilder, *, frame: int, color: bool,
    width: int | None = None, now_iso: str | None = None,
) -> str | None:
    """Single combined status line: the activity line joined with the position bar.
    The streaming live view uses the multi-row footer instead."""
    activity = builder.current_activity()
    if activity is None:
        return None
    if now_iso is None:
        now_iso = datetime.now(UTC).isoformat()
    root = builder.tree.root
    w = term_width() if width is None else width
    bar = compose_position_bar(
        activity=activity, quest_id=root.id, title=root.title,
        prompt_tokens=builder.latest_prompt_tokens, color=color, width=w,
        context_pct=builder.latest_context_pct,
        checkpoint_threshold=builder.latest_checkpoint_threshold, pad_slate=False)
    line = compose_activity_line(activity=activity, frame=frame, color=color, now_iso=now_iso)
    return rt.compose_label([line, bar])


@dataclass
class _Cursor:
    container: Any   # WorkflowNode | PhaseNode | StepNode | SubagentFold
    depth: int       # depth at which this container's CHILDREN render
    index: int = 0   # next unemitted child


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
                 quest_id: str | None = None, title: str = "") -> None:
        self._builder = nv.NarrativeBuilder()
        if quest_id is not None:
            self._builder.seed_identity(quest_id, title)
        self._stream = stream
        self._color = color
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
        self._emitted_any = False
        self._last_header_position: str | None = None
        self._deferred: list[tuple[list[str], bool, str]] = []
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

    def append_committed_reply(self, text: str) -> None:
        # Committed, non-reconciling echo for a control slash command (no daemon
        # user_message will arrive). Mirrors seed_pending_reply's walk/refresh but
        # arms no _pending_reply_node.
        self._builder.append_committed_reply(text)
        self._walk()
        self._refresh_status()

    def seed_opening_intent(self, text: str) -> None:
        # Optimistic local echo at submit time; mirrors seed_pending_reply (emits via _walk).
        self._builder.seed_opening_intent(text)
        # Non-TUI only: arm the pre-boot footer spinner (superseded once a step starts).
        self._builder.seed_first_spinner()
        self._walk()
        self._refresh_status()

    def finish(self) -> None:
        """Run end / pause / error: force-flush any still-open burst/subagent so
        the final block is emitted, then clear the status line."""
        self._builder.finalize()
        self._walk(force=True)
        self._status.reset()

    def compose_status(self, frame: int) -> list[str] | None:
        activity = self._builder.current_activity()
        if activity is None:
            return None
        now_iso = datetime.now(UTC).isoformat()
        a_line = compose_activity_line(activity=activity, frame=frame,
                                       color=self._color, now_iso=now_iso)
        root = self._builder.tree.root
        w = term_width()
        p_row = compose_position_row(
            activity=activity, quest_id=root.id, title=root.title,
            prompt_tokens=self._builder.latest_prompt_tokens,
            color=self._color, width=w,
            context_pct=self._builder.latest_context_pct,
            checkpoint_threshold=self._builder.latest_checkpoint_threshold)
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
            frame=frame, color=self._color, now_iso=now_iso)
        root = self._builder.tree.root
        identity_row = compose_identity_toolbar(
            quest_id=root.id, title=root.title,
            color=self._color, width=term_width())
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
                quest_id=root.id, title=root.title,
                color=self._color, width=term_width())
        return compose_position_row(
            activity=activity, quest_id=root.id, title=root.title,
            prompt_tokens=self._builder.latest_prompt_tokens,
            color=self._color, width=term_width(),
            context_pct=self._builder.latest_context_pct,
            checkpoint_threshold=self._builder.latest_checkpoint_threshold)

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
                self._frontier.append(_Cursor(container=child, depth=child_depth))
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
            self._prev_prose = False

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
        # A held AgentTextNode is settled narration: it emits immediately and is
        # never withheld behind an in-flight tool call. `held` still drives
        # `imp log`'s static fold rendering; only the live cursor ignores it.
        return True

    def _gutters(self) -> tuple[int, ...]:
        """Subagent boundary columns for the shared render helpers.

        Part D made SubagentFold a leaf: it is emitted via _emit_leaf and never
        pushed onto the frontier as a cursor. Only PhaseNode/StepNode containers
        are pushed (_is_container). Therefore there are no SubagentFold cursors
        in self._frontier during the live walk and this always returns (). The
        magenta │ gutter appears only in the imp log --full expanded subagent
        view (render_text), not in live/lean."""
        return ()

    def _emit_leaf(self, node: Any, depth: int,
                   gutters: tuple[int, ...] = ()) -> None:
        if isinstance(node, (nv.MergedNode, nv.CompletedNode)):
            # suppressed in live path — main.py _emit_completed owns the friendly line
            return
        if rt.spaces_before(node):
            # A leaf that wants a blank line above it (an error) gets one even when
            # its predecessor wasn't a spaced block — mirrors render_text's
            # spaces_before check in _render_children.
            self._prev_prose = True
        if isinstance(node, nv.SubagentFold):
            # Part D: a subagent collapses to a single resolved status row — no
            # streamed children, no outcome. Shares render_text.subagent_status_row
            # with the lean view so live and imp log stay byte-identical.
            self._print_lines(
                [rt.subagent_status_row(node, depth=depth, color=self._color,
                                        gutters=gutters)],
                prose=rt.spaces_after(node, full=False),
                separator_indent=rt._gutter_indent(depth, gutters, self._color))
            return
        if isinstance(node, nv.UserMessageNode):
            # The interactive prompt uses erase_when_done, so prompt_toolkit leaves
            # NO echo in scroll-back on a real terminal — we reprint the turn here
            # (no longer suppressed on a TTY). Rendered as a user turn: flush-left,
            # marked by the bright-blue `> ` caret,
            # spaced like a chat message. The same path serves a non-TTY sink
            # (offline capture / `imp run | tee` / the snap-ui mockup), so live and
            # `imp log` show the user's turn identically.
            lines = rt._render(node, depth=depth, full=False, color=self._color,
                               width=self._cur_width(),
                               display_width=self._cur_display_width(),
                               gutters=gutters)
            self._print_lines(
                list(lines), prose=True,
                separator_indent=rt._gutter_indent(depth, gutters, self._color))
            return
        lines = rt._render(node, depth=depth, full=False, color=self._color,
                           width=self._cur_width(),
                           display_width=self._cur_display_width(),
                           gutters=gutters)
        prose = rt.spaces_after(node, full=False)
        self._print_lines(
            list(lines), prose=prose,
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
