# src/imp/cli/live_driver.py
"""Live run-view driver: emit-on-finalize folded rendering.

Holds a NarrativeBuilder (the same one imp log's build_tree uses) and, after each
event, emits the nodes that are now final — in tree (DFS) order — through the
folded render_text._render_* helpers. The live view and imp log therefore share
one render path (design Part 2).

Ordering, dedup, and depth come from a driver-owned STACK of frontier cursors —
one per open container the driver is tracking (root -> phase -> step -> agent-gate
step). Each cursor points at the next unemitted child of its container, advances
left-to-right, emits a child once when final, and blocks on a not-final child.
Depth is read from the cursor stack, NOT the builder's mutable _step_stack /
_current_phase (a scope-closer event pops those BEFORE this post-add() walk runs).

Not-final children (the only things that block a cursor):
  - an open ToolBurstFold (still its owning builder's current_burst; for a
    nested burst the owner is the subagent's nested builder, not the main one),
  - an unresolved human GateNode (action is None) — deferred until the human
    step's step_completed stamps the resolved route onto it.
Everything else is final on append. A SubagentFold is a LEAF (Part D): it
collapses to a single `↳ task → {name} ⟨status⟩` status row emitted once the
subagent stops running — its streamed children and structured return value are
never rendered in the live/lean views (this structurally eliminates the SDK
cancellation-sentinel leak).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from imp.cli import narrative as nv
from imp.cli import palette as pal
from imp.cli import render_text as rt
from imp.cli.live_status import LiveStatus

_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


@dataclass
class _Cursor:
    container: Any   # WorkflowNode | PhaseNode | StepNode | SubagentFold
    depth: int       # depth at which this container's CHILDREN render
    index: int = 0   # next unemitted child


def _is_container(node: Any) -> bool:
    return isinstance(node, (nv.PhaseNode, nv.StepNode))


def _container_children(container: Any) -> list[Any]:
    children: list[Any] = container.children
    return children


def _container_closed(container: Any) -> bool:
    return getattr(container, "closed", False)


class LiveDriver:
    def __init__(self, *, stream: Any, color: bool, width: int, status: LiveStatus) -> None:
        self._builder = nv.NarrativeBuilder()
        self._stream = stream
        self._color = color
        self._width = width
        self._status = status
        # True after the last emitted block was agent prose, so the next prose
        # block gets one blank-line separator — matching render_text's lean spacing
        # so the live view and `imp log` look identical (design §5.4). Any non-prose
        # output resets it; an empty emission leaves it untouched.
        self._prev_prose = False
        # root cursor: render root's children at depth 0 (matches render_tree).
        self._frontier: list[_Cursor] = [
            _Cursor(container=self._builder.tree.root, depth=0)]

    # -- engine callbacks -----------------------------------------------------
    def on_event(self, event: Any) -> None:
        self._builder.add(event)
        if isinstance(event, dict) and event.get("type") in (
                "step_started", "user_message"):
            # §7: time-the-agent-working-since-the-last-user-reply. Reset on
            # step_started AND user_message so the elapsed counter measures
            # the current burst between user inputs (across tools, thinks, and
            # multiple llm_response blocks within one interactive turn). Non-
            # interactive runs never fire user_message, so today's per-step
            # reset behavior is preserved as a side effect.
            self._status.restart_span()
        self._walk()
        self._refresh_status()

    def print_chrome(self, text: str) -> None:
        """human_output_fn sink: assistant->user chat text (and gate chrome).
        Live-only — no event/node — so its emphasis lives here, styled to match
        LLM prose. Clears the status line first; the next event re-shows it."""
        self._status.hide()
        for ln in str(text).split("\n"):
            self._stream.write(
                pal.ansi(ln, *pal.NODE_COLORS["agent"], enabled=self._color) + "\n")
        self._stream.flush()

    def finish(self) -> None:
        """Run end / pause / error: force-flush any still-open burst/subagent (and
        any unresolved gate) so the final block is emitted, then clear the status
        line."""
        self._walk(force=True)
        self._status.reset()

    def compose_status(self, frame: int, elapsed: float) -> str | None:
        activity = self._builder.current_activity()
        if activity is None:
            return None
        # cyan spinner draws the eye; the elapsed tail dims so it doesn't compete
        # with the activity text. Plain when color is off (byte-identical to before).
        spin = pal.ansi(_SPINNER[frame % len(_SPINNER)], "cyan", enabled=self._color)
        tail = pal.ansi(f"· {rt.fmt_seconds(elapsed)}", "dim", enabled=self._color)
        return f"{spin} {activity} {tail}"

    # -- emission -------------------------------------------------------------
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
                          and nv.phase_elapsed(cur.container) is not None):
                        # The phase-complete line is surrounded by blanks: arm
                        # prev_prose for the blank ABOVE, and pass prose=True so the
                        # next phase header gets the blank BELOW (matching the batch
                        # walk's spaces_before/after(PhaseNode)).
                        self._prev_prose = True
                        self._print_lines(
                            [rt.phase_breadcrumb_line(
                                cur.container, depth=cur.depth - 1, color=self._color,
                                gutters=self._gutters())],
                            prose=True,
                            separator_indent=rt._gutter_indent(
                                cur.depth - 1, self._gutters(), self._color))
                    self._frontier.pop()        # container drained + closed
                    continue
                return                          # open container, no new children yet
            child = children[cur.index]
            if not force and not self._is_final(child, container=cur.container):
                return                          # block on a not-final child
            cur.index += 1
            if _is_container(child):
                # PhaseNode/StepNode: print the header now and descend, streaming
                # the container's children as their events route in. A phase
                # indents its steps (+1); a step's content flows at the step's
                # OWN depth (no +1) so prose aligns under the header instead of
                # burying it — must match render_text._render. (aligned indent)
                gutters = self._gutters()
                if rt.is_chrome_step(child):
                    # A no-op gate/script step has no header — it collapses to the
                    # single dark-gray breadcrumb its step_completed appends. Skip
                    # the header line; descend so that breadcrumb still streams. It's
                    # surrounded like a leaf, so arm the blank-above here (its
                    # breadcrumb provides the blank-below via spaces_after).
                    if rt.spaces_before(child):
                        self._prev_prose = True
                else:
                    if rt.spaces_before(child):
                        # A phase header and an interactive step header get a blank
                        # line above them (matching render_text._render_children).
                        # Arming the prose-adjacency flag makes the next emission —
                        # the header — carry the leading separator.
                        self._prev_prose = True
                    self._stream_or_hold(
                        rt.header_line(child, depth=cur.depth, color=self._color,
                                       gutters=gutters),
                        depth=cur.depth, gutters=gutters)
                    if isinstance(child, (nv.StepNode, nv.PhaseNode)):
                        # One blank line below the header: between an agent step's
                        # header and its first words, and between a phase's start
                        # line and its first step (surrounding the phase start).
                        # Arm the prose-adjacency flag so the next real emission
                        # carries a leading separator; the dropped lean context
                        # line is an empty emission that leaves this untouched, so
                        # the blank lands before the real content.
                        self._prev_prose = True
                child_depth = cur.depth + 1 if isinstance(child, nv.PhaseNode) else cur.depth
                self._frontier.append(_Cursor(container=child, depth=child_depth))
            else:
                self._emit_leaf(child, cur.depth, gutters=self._gutters())

    def _is_final(self, node: Any, *, container: Any) -> bool:
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
        if isinstance(node, nv.GateNode) and node.kind == "human":
            return node.action is not None     # deferred until step_completed
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
            # bright-blue caret, subtle "entered input" background (render_text),
            # spaced like a chat message. The same path serves a non-TTY sink
            # (offline capture / `imp run | tee` / the snap-ui mockup), so live and
            # `imp log` show the user's turn identically.
            lines = rt._render(node, depth=depth, full=False, color=self._color,
                               width=self._width, gutters=gutters)
            self._print_lines(
                list(lines), prose=True,
                separator_indent=rt._gutter_indent(depth, gutters, self._color))
            return
        if isinstance(node, nv.GateNode) and node.kind == "human":
            # Live renders the resolved header only; run_human_gate already showed
            # the prompt/options as chrome (design "Human gates").
            lines = rt._render_gate(node, depth=depth, color=self._color,
                                    width=self._width, recap=False, gutters=gutters)
            # Spaced like the batch walk (blank after the gate block) so the
            # step-completion breadcrumb that follows stands clear of it.
            prose = rt.spaces_after(node, full=False)
        else:
            lines = rt._render(node, depth=depth, full=False, color=self._color,
                               width=self._width, gutters=gutters)
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
        # pairing).
        if not lines:
            return
        self._status.hide()
        if self._prev_prose:
            self._stream.write(separator_indent + "\n")
        for ln in lines:
            self._stream.write(ln + "\n")
        self._stream.flush()
        self._prev_prose = prose

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
        if self._builder.current_activity() is not None:
            self._status.update(self.compose_status)
        else:
            self._status.reset()
