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
Everything else is final on append. A SubagentFold is NOT a blocking leaf: it is
a descended container (like a PhaseNode/StepNode), so its children stream live as
their events route in, and its structured return value is emitted on close.
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
    return isinstance(node, (nv.PhaseNode, nv.StepNode, nv.SubagentFold))


def _container_children(container: Any) -> list[Any]:
    # A SubagentFold's children live on its nested tree, not a `.children` attr.
    if isinstance(container, nv.SubagentFold):
        children: list[Any] = container.tree.root.children
    else:
        children = container.children
    return children


def _container_closed(container: Any) -> bool:
    # A SubagentFold has no `closed` attr; "closed" means it stopped running.
    if isinstance(container, nv.SubagentFold):
        return not container.running
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
        if isinstance(event, dict) and event.get("type") == "change_created":
            self._emit_header()
        if isinstance(event, dict) and event.get("type") == "step_started":
            self._status.restart_span()      # §7: time the current step's span
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
        spin = _SPINNER[frame % len(_SPINNER)]
        return f"{spin} {activity} · {int(elapsed)}s"

    # -- emission -------------------------------------------------------------
    def _emit_header(self) -> None:
        root = self._builder.tree.root
        head = (f"#{root.slug if root.slug is not None else '?'}  "
                f"{root.title}").rstrip()
        self._print_lines([pal.ansi(head, *pal.NODE_COLORS["workflow"], enabled=self._color)])

    def _walk(self, *, force: bool = False) -> None:
        while self._frontier:
            cur = self._frontier[-1]
            children = _container_children(cur.container)
            if cur.index >= len(children):
                if len(self._frontier) == 1:
                    return                      # root: never pop; await more children
                if force or _container_closed(cur.container):
                    # A drained+closed subagent emits its return value before pop,
                    # at the prose depth (cur.depth) so it matches imp log. A still-
                    # running subagent force-popped at finish() has an empty outcome
                    # and emits nothing.
                    if isinstance(cur.container, nv.SubagentFold):
                        self._print_lines(list(rt.subagent_outcome_lines(
                            cur.container, depth=cur.depth, color=self._color,
                            width=self._width, gutters=self._gutters()
                        )))
                    elif (isinstance(cur.container, nv.PhaseNode)
                          and _container_closed(cur.container)
                          and nv.phase_elapsed(cur.container) is not None):
                        self._print_lines([rt.phase_breadcrumb_line(
                            cur.container, depth=cur.depth - 1, color=self._color,
                            gutters=self._gutters())])
                    self._frontier.pop()        # container drained + closed
                    continue
                return                          # open container, no new children yet
            child = children[cur.index]
            if not force and not self._is_final(child, container=cur.container):
                return                          # block on a not-final child
            cur.index += 1
            if _is_container(child):
                # Subagent header is the shared `↳ task → {name}` boundary; the live
                # driver prints it the instant the subagent starts (while running),
                # then streams the subagent's children as their events route in.
                self._stream_or_hold(rt.header_line(
                    child, depth=cur.depth, color=self._color, gutters=self._gutters()))
                self._frontier.append(_Cursor(container=child, depth=cur.depth + 1))
            else:
                self._emit_leaf(child, cur.depth, gutters=self._gutters())

    def _is_final(self, node: Any, *, container: Any) -> bool:
        if isinstance(node, nv.ToolBurstFold):
            # A burst is "open" (not yet final) only against the current_burst of
            # the builder that OWNS it: a top-level burst belongs to the main
            # builder; a *nested* subagent's burst belongs to that subagent's
            # nested builder. Comparing against the owning builder keeps this
            # correct regardless of whether render_text renders bursts in
            # full=False — no reliance on the A2 drop.
            if isinstance(container, nv.SubagentFold):
                # Once the subagent has closed, no more nested events arrive, so a
                # still-"open" nested burst is final — otherwise the cursor would
                # block here forever and the subagent's outcome would never emit on
                # the normal (non-force) walk.
                return (not container.running
                        or node is not container.nested.current_burst)
            return node is not self._builder.current_burst
        if isinstance(node, nv.GateNode) and node.kind == "human":
            return node.action is not None     # deferred until step_completed
        # SubagentFold is no longer a blocking leaf — it is a descended container
        # (see _is_container / _walk), streamed child-by-child as events arrive.
        return True

    def _gutters(self) -> tuple[int, ...]:
        """Set of indent columns that are subagent boundaries, read from the
        cursor stack. A SubagentFold cursor's `.depth` is its CHILDREN depth, so
        the subagent's own header column is `.depth - 1`. The self-inclusion rule
        falls out of WHEN this is called: the header is emitted BEFORE self's
        cursor is pushed (so self is excluded), and the outcome is emitted BEFORE
        self's cursor is popped (so self is included) — matching imp log.

        Single-level routing (spawned subagents get subagent_defs={}) means a live
        nested subagent can't occur, so this returns a 0- or 1-tuple in practice;
        the multi-entry/ordering behavior is covered by the imp log nested test
        (test_nested_subagent_emits_two_gutter_levels), which builds the nested
        fold directly — there is no live nested test."""
        return tuple(c.depth - 1 for c in self._frontier
                     if isinstance(c.container, nv.SubagentFold))

    def _emit_leaf(self, node: Any, depth: int,
                   gutters: tuple[int, ...] = ()) -> None:
        if isinstance(node, nv.UserMessageNode):
            # §4: live already showed the user's input via prompt_toolkit's raw
            # echo; re-emitting the guttered node would duplicate it. imp log,
            # which has no terminal echo, still renders the node normally.
            # _prev_prose is intentionally left untouched — the node isn't a
            # prose leaf, so the early return won't disturb prose blank-line spacing.
            return
        if isinstance(node, nv.GateNode) and node.kind == "human":
            # Live renders the resolved header only; run_human_gate already showed
            # the prompt/options as chrome (design "Human gates").
            lines = rt._render_gate(node, depth=depth, color=self._color,
                                    width=self._width, recap=False, gutters=gutters)
            prose = False
        else:
            lines = rt._render(node, depth=depth, full=False, color=self._color,
                               width=self._width, gutters=gutters)
            prose = rt.is_prose_leaf(node)
        self._print_lines(list(lines), prose=prose)

    def _stream_or_hold(self, line: str) -> None:
        self._print_lines([line])

    def _print_lines(self, lines: list[str], *, prose: bool = False) -> None:
        # The ONE output chokepoint, so prose spacing stays consistent: a blank
        # line precedes a prose block that follows another prose block. An empty
        # emission returns early and leaves _prev_prose untouched (so a lean-mode
        # thinking/tool turn between two prose blocks doesn't break their pairing).
        if not lines:
            return
        self._status.hide()
        if prose and self._prev_prose:
            self._stream.write("\n")
        for ln in lines:
            self._stream.write(ln + "\n")
        self._stream.flush()
        self._prev_prose = prose

    def _refresh_status(self) -> None:
        if self._builder.current_activity() is not None:
            self._status.update(self.compose_status)
        else:
            self._status.reset()
