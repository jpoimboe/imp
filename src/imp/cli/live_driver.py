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
  - an open ToolBurstFold (still builder.current_burst),
  - a SubagentFold with running == True,
  - an unresolved human GateNode (action is None) — deferred until the human
    step's step_completed stamps the resolved route onto it.
Everything else is final on append.
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
    container: Any   # WorkflowNode | PhaseNode | StepNode
    depth: int       # depth at which this container's CHILDREN render
    index: int = 0   # next unemitted child


def _is_container(node: Any) -> bool:
    return isinstance(node, (nv.PhaseNode, nv.StepNode))


class LiveDriver:
    def __init__(self, *, stream: Any, color: bool, width: int, status: LiveStatus) -> None:
        self._builder = nv.NarrativeBuilder()
        self._stream = stream
        self._color = color
        self._width = width
        self._status = status
        # root cursor: render root's children at depth 0 (matches render_tree).
        self._frontier: list[_Cursor] = [
            _Cursor(container=self._builder.tree.root, depth=0)]

    # -- engine callbacks -----------------------------------------------------
    def on_event(self, event: Any) -> None:
        self._builder.add(event)
        if isinstance(event, dict) and event.get("type") == "change_created":
            self._emit_header()
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
            children = cur.container.children
            if cur.index >= len(children):
                if len(self._frontier) == 1:
                    return                      # root: never pop; await more children
                if force or getattr(cur.container, "closed", False):
                    self._frontier.pop()        # container drained + closed
                    continue
                return                          # open container, no new children yet
            child = children[cur.index]
            if not force and not self._is_final(child):
                return                          # block on a not-final child
            cur.index += 1
            if _is_container(child):
                self._stream_or_hold(rt.header_line(
                    child, depth=cur.depth, color=self._color))
                self._frontier.append(_Cursor(container=child, depth=cur.depth + 1))
            else:
                self._emit_leaf(child, cur.depth)

    def _is_final(self, node: Any) -> bool:
        if isinstance(node, nv.ToolBurstFold):
            return node is not self._builder.current_burst
        if isinstance(node, nv.SubagentFold):
            return not node.running
        if isinstance(node, nv.GateNode) and node.kind == "human":
            return node.action is not None     # deferred until step_completed
        return True

    def _emit_leaf(self, node: Any, depth: int) -> None:
        if isinstance(node, nv.GateNode) and node.kind == "human":
            # Live renders the resolved header only; run_human_gate already showed
            # the prompt/options as chrome (design "Human gates").
            lines = rt._render_gate(node, depth=depth, color=self._color,
                                    width=self._width, recap=False)
        else:
            lines = rt._render(node, depth=depth, full=False, color=self._color,
                               width=self._width)
        self._print_lines(list(lines))

    def _stream_or_hold(self, line: str) -> None:
        self._print_lines([line])

    def _print_lines(self, lines: list[str]) -> None:
        if not lines:
            return
        self._status.hide()
        for ln in lines:
            self._stream.write(ln + "\n")
        self._stream.flush()

    def _refresh_status(self) -> None:
        if self._builder.current_activity() is not None:
            self._status.update(self.compose_status)
        else:
            self._status.reset()
