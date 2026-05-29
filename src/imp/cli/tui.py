# src/imp/cli/tui.py
"""Interactive TUI: a prompt_toolkit Application rendering a NarrativeTree via
rich. rich + prompt_toolkit are confined to this module (design §7.2).

This file is layered:
  - FoldState / RenderedLine / render_tree_to_lines  — pure, testable
  - cursor + scroll helpers                          — pure, testable
  - the Application shell                             — thin, smoke-tested
"""
from __future__ import annotations

import asyncio
import contextlib
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from imp.cli import narrative as nv
from imp.cli import palette as pal

if TYPE_CHECKING:
    from prompt_toolkit.application import Application
    from prompt_toolkit.buffer import Buffer

# StyleAndTextTuples: each fragment is (style, text) or (style, text, handler)
# We use list[Any] to accommodate both 2-tuples and 3-tuples (mouse handlers).
Fragments = list[Any]


@dataclass
class RenderedLine:
    fragments: Fragments                 # list[(style, text)] — color via ANSI()
    node: Any                            # owning node (cursor→node, fold toggle)
    is_fold_header: bool = False


class FoldState:
    """Per-node expand/collapse. Default-collapsed unless default_expanded
    (used by --full / expand-all); individual toggles override the default."""

    def __init__(self, default_expanded: bool = False) -> None:
        self.default_expanded = default_expanded
        self._expanded: set[int] = set()      # exceptions when default collapsed
        self._collapsed: set[int] = set()     # exceptions when default expanded

    def is_expanded(self, node: Any) -> bool:
        nid = id(node)
        if self.default_expanded:
            return nid not in self._collapsed
        return nid in self._expanded

    def toggle(self, node: Any) -> None:
        nid = id(node)
        if self.is_expanded(node):
            if self.default_expanded:
                self._collapsed.add(nid)
            else:
                self._expanded.discard(nid)
        else:
            if self.default_expanded:
                self._collapsed.discard(nid)
            else:
                self._expanded.add(nid)

    def set_all(self, expanded: bool) -> None:
        self.default_expanded = expanded
        self._expanded.clear()
        self._collapsed.clear()


_INDENT = "  "


class AnsiMemo:
    """Bounded LRU cache of per-(node_id, width) rendered ANSI line lists.

    Content nodes are immutable (design §7.2), so an entry is valid until the
    width changes — a resize drops width-mismatched entries; only NEW nodes are
    rendered. Bounded so long sessions / repeated resizes can't grow it without
    limit."""

    def __init__(self, max_entries: int = 512) -> None:
        self._d: OrderedDict[tuple[int, int], list[str]] = OrderedDict()
        self.max_entries = max_entries
        self.hits = 0

    @property
    def size(self) -> int:
        return len(self._d)

    def get(self, key: tuple[int, int]) -> list[str] | None:
        v = self._d.get(key)
        if v is not None:
            self.hits += 1
            self._d.move_to_end(key)
        return v

    def put(self, key: tuple[int, int], value: list[str]) -> None:
        self._d[key] = value
        self._d.move_to_end(key)
        while len(self._d) > self.max_entries:
            self._d.popitem(last=False)


def _ansi_to_fragments(ansi_str: str) -> Fragments:
    """Convert an ANSI-coded string to prompt_toolkit fragments."""
    from prompt_toolkit.formatted_text import ANSI
    return list(ANSI(ansi_str).__pt_formatted_text__())


def _plain(text: str) -> Fragments:
    return [("", text)]


def render_tree_to_lines(tree: nv.NarrativeTree, fold_state: FoldState,
                         width: int, *, color: bool = True,
                         memo: AnsiMemo | None = None) -> list[RenderedLine]:
    """Pure: NarrativeTree + fold state + width → ordered RenderedLines.

    `memo` (optional) caches per-(node,width) rich/ANSI output for prose nodes
    (design §7.2 / F5); pass None in tests that don't care about caching."""
    # PERF (design §7.7): this does a full O(n) walk per invalidate. The AnsiMemo
    # caches per-node rich cost, but not this walk. Fine at imp's scale; if a
    # long streaming change regresses, switch to dirty-subtree line-list
    # assembly (rebuild only the changed subtree, splice into a cached list).
    out: list[RenderedLine] = []
    root = tree.root
    head = f"#{root.change_id if root.change_id is not None else '?'}  {root.title}".rstrip()
    if root.status != "running":
        head += f"  ({root.status})"
    out.append(RenderedLine(
        fragments=_ansi_to_fragments(pal.ansi(head, *pal.NODE_COLORS["workflow"], enabled=color)),
        node=root))
    for child in root.children:
        _emit(out, child, depth=0, fs=fold_state, width=width, color=color, memo=memo)
    return out


def _line(out: list[RenderedLine], text: str, depth: int, node: Any, color: bool,
          *codes: str, is_fold_header: bool = False) -> None:
    s = pal.ansi(f"{_INDENT * depth}{text}", *codes, enabled=color)
    out.append(RenderedLine(fragments=_ansi_to_fragments(s), node=node,
                            is_fold_header=is_fold_header))


def _emit(out: list[RenderedLine], node: Any, *, depth: int, fs: FoldState,
          width: int, color: bool, memo: AnsiMemo | None) -> None:
    if isinstance(node, nv.PhaseNode):
        _line(out, node.name, depth, node, color, *pal.NODE_COLORS["phase"])
        for c in node.children:
            _emit(out, c, depth=depth + 1, fs=fs, width=width, color=color, memo=memo)
        return
    if isinstance(node, nv.StepNode):
        label = f"agent_gate: {node.name}" if node.kind == "agent_gate" else node.name
        if node.interactive:
            label += " (interactive)"
        _line(out, label, depth, node, color, *pal.NODE_COLORS["step"])
        for c in node.children:
            _emit(out, c, depth=depth + 1, fs=fs, width=width, color=color, memo=memo)
        return
    if isinstance(node, (nv.AgentTextNode, nv.UserMessageNode)):
        for frags in _prose_lines(node, width, depth, color, memo):
            out.append(RenderedLine(fragments=frags, node=node))
        return
    if isinstance(node, nv.OutcomeLine):
        ac = pal.accent_color(node.action)
        _line(out, f"→ {pal.action_text(node.action)}", depth, node, color,
              *((ac,) if ac else ()))
        return
    if isinstance(node, nv.ErrorNode):
        if node.is_warning:
            _line(out, f"warning: {node.message}", depth, node, color, *pal.NODE_COLORS["warning"])
        else:
            tag = f"[{node.category}] " if node.category else ""
            _line(out, f"error: {tag}{node.message}", depth, node, color, *pal.NODE_COLORS["error"])
        return
    if isinstance(node, nv.GateNode):
        action = pal.action_text(node.action) if node.action is not None else "pending"
        _line(out, f"{node.kind} gate → {action}", depth, node, color, *pal.NODE_COLORS["gate"])
        if node.prompt:
            _line(out, node.prompt, depth + 1, node, color, *pal.NODE_COLORS["gate"])
            for opt in node.options:
                _line(out, f"- {opt.get('label', opt)}", depth + 1, node, color,
                      *pal.NODE_COLORS["gate"])
        return
    if isinstance(node, nv.FallbackLine):
        kv = " ".join(f"{k}={v}" for k, v in node.payload.items())
        _line(out, f"{node.event_type}  {kv}".rstrip(), depth, node, color, "dim")
        return
    # collapsible folds
    if isinstance(node, (nv.ThinkingFold, nv.ToolBurstFold, nv.SubagentFold,
                         nv.StepContextNode)):
        _emit_fold(out, node, depth=depth, fs=fs, width=width, color=color, memo=memo)
        return


def _emit_fold(out: list[RenderedLine], node: Any, *, depth: int, fs: FoldState,
               width: int, color: bool, memo: AnsiMemo | None) -> None:
    expanded = fs.is_expanded(node)
    glyph = pal.fold_glyph(expanded=expanded)
    if isinstance(node, nv.ThinkingFold):
        if expanded:
            _line(out, f"{glyph} thinking", depth, node, color,
                  *pal.NODE_COLORS["thinking"], is_fold_header=True)
            for frags in _wrap_fragments(node.content, width, depth + 1, color,
                                         pal.NODE_COLORS["thinking"]):
                out.append(RenderedLine(fragments=frags, node=node))
        else:
            _line(out, f"{glyph} thinking · {len(node.content)} chars", depth, node,
                  color, *pal.NODE_COLORS["thinking"], is_fold_header=True)
        return
    if isinstance(node, nv.ToolBurstFold):
        if expanded:
            _line(out, f"{glyph} tools", depth, node, color, *pal.NODE_COLORS["tools"],
                  is_fold_header=True)
            for call in node.calls:
                args = " ".join(f"{k}={v}" for k, v in (call.input or {}).items())
                killed = " [killed]" if call.killed else ""
                res = (call.result or "").replace("\n", " ")
                if len(res) > 60:
                    res = res[:60] + "…"
                for frags in _wrap_fragments(f"⏺ {call.name}  {args}  → {res}{killed}".rstrip(),
                                             width, depth + 1, color, pal.NODE_COLORS["tools"]):
                    out.append(RenderedLine(fragments=frags, node=node))
        else:
            from imp.cli.render_text import _burst_counts
            _line(out, f"{glyph} {len(node.calls)} tools · {_burst_counts(node)}", depth,
                  node, color, *pal.NODE_COLORS["tools"], is_fold_header=True)
        return
    if isinstance(node, nv.SubagentFold):
        if expanded:
            _line(out, f"{glyph} task → {node.subagent_name}", depth, node, color,
                  *pal.NODE_COLORS["subagent"], is_fold_header=True)
            for c in node.tree.root.children:
                _emit(out, c, depth=depth + 1, fs=fs, width=width, color=color, memo=memo)
        else:
            tail = "running" if node.running else f"→ {(node.outcome or '').strip()[:40]}"
            _line(out, f"{glyph} task → {node.subagent_name} · {node.event_count} events · {tail}",
                  depth, node, color, *pal.NODE_COLORS["subagent"], is_fold_header=True)
        return
    if isinstance(node, nv.StepContextNode):
        p = node.payload
        if expanded:
            _line(out, f"{glyph} context · {p.get('provider', '?')}/{p.get('model', '?')}",
                  depth, node, color, *pal.NODE_COLORS["metadata"], is_fold_header=True)
            for frags in _wrap_fragments("system_prompt: " + str(p.get("system_prompt", "")),
                                         width, depth + 1, color, pal.NODE_COLORS["metadata"]):
                out.append(RenderedLine(fragments=frags, node=node))
            for t in (p.get("tools_schema") or []):
                name = t.get("name", t) if isinstance(t, dict) else t
                _line(out, f"- {name}", depth + 1, node, color, *pal.NODE_COLORS["metadata"])
        else:
            tools = p.get("tools_schema") or []
            _line(out, f"{glyph} context · {p.get('provider', '?')}/{p.get('model', '?')} · "
                  f"{len(tools)} tools", depth, node, color, *pal.NODE_COLORS["metadata"],
                  is_fold_header=True)
        return


def _wrap_fragments(text: str, width: int, depth: int, color: bool,
                    codes: tuple[str, ...]) -> list[Fragments]:
    import textwrap
    indent = _INDENT * depth
    avail = max(8, width - len(indent))
    out: list[Fragments] = []
    for para in text.split("\n"):
        for line in (textwrap.wrap(para, width=avail) or [""]):
            out.append(_ansi_to_fragments(pal.ansi(f"{indent}{line}", *codes, enabled=color)))
    return out


def _prose_lines(node: Any, width: int, depth: int, color: bool,
                 memo: AnsiMemo | None) -> list[Fragments]:
    indent_cols = len(_INDENT) * depth
    key = (id(node), width)
    cached = memo.get(key) if memo is not None else None
    if cached is None:
        cached = _render_prose_ansi(node, width - indent_cols, color)
        if memo is not None:
            memo.put(key, cached)
    pad = _INDENT * depth
    return [_ansi_to_fragments(pad + line) for line in cached]


def fold_header_indices(lines: list[RenderedLine]) -> list[int]:
    return [i for i, ln in enumerate(lines) if ln.is_fold_header]


def next_fold_line(lines: list[RenderedLine], cursor: int) -> int:
    headers = fold_header_indices(lines)
    for i in headers:
        if i > cursor:
            return i
    return headers[-1] if headers else cursor


def prev_fold_line(lines: list[RenderedLine], cursor: int) -> int:
    headers = fold_header_indices(lines)
    for i in reversed(headers):
        if i < cursor:
            return i
    return headers[0] if headers else cursor


def remap_cursor_to_node(lines: list[RenderedLine], node: Any, *, fallback: int) -> int:
    """After a fold toggle / resize, find the line owning `node` (prefer its
    fold header). Cursor follows the logical node, not the line number
    (design §7.3)."""
    header = next((i for i, ln in enumerate(lines)
                   if ln.node is node and ln.is_fold_header), None)
    if header is not None:
        return header
    any_line = next((i for i, ln in enumerate(lines) if ln.node is node), None)
    if any_line is not None:
        return any_line
    return min(fallback, len(lines) - 1) if lines else 0


def is_at_tail(cursor: int, line_count: int) -> bool:
    return cursor >= line_count - 1


class NarrativeApp:
    """The full-screen narrative Application. `live=True` adds the input bar and
    sticky tail (M5); `live=False` is read-only log mode."""

    def __init__(self, tree: nv.NarrativeTree, *, live: bool, mouse: bool,
                 full: bool = False, _osc_writer: Any = None,
                 builder: nv.NarrativeBuilder | None = None,
                 coordinator: Any = None) -> None:
        # emission thread: on-loop, verified 2026-05-28 (grep found no
        # to_thread/run_in_executor/Thread usage in providers/agents/engine)
        self.live = live
        self.mouse = mouse
        self.fold_state = FoldState(default_expanded=full)
        self.memo = AnsiMemo()
        self.cursor_line = 0
        self.follow_tail = True               # sticky tail (live); harmless in log
        self._width = 80
        self._lines: list[RenderedLine] = []
        self.status_note = ""
        self._osc_writer = _osc_writer        # injected in tests; real one in build
        self.coordinator = coordinator
        self.exit_reason: str | None = None   # None | "pause" | "eof" | "done"
        self._pausing = False
        # For live mode, hold the builder so on_event can feed it incrementally;
        # the tree is then a live view over builder.tree (design §7.7).
        # If no builder is supplied in live mode, create a fresh one so callers
        # can use on_event without pre-constructing a builder.
        self._builder: nv.NarrativeBuilder | None
        if live:
            self._builder = builder if builder is not None else nv.NarrativeBuilder()
            self.tree = self._builder.tree
        else:
            self._builder = None
            self.tree = tree
        self.new_below_count = 0
        # live input state (design §7.5)
        self._active_input: asyncio.Future[str] | None = None
        self._input_prompt = "> "
        # human-gate inline selector state (design §7.5 / Task 31)
        self._gate_active = False
        self._gate_options: list[Any] = []
        self._gate_future: asyncio.Future[str] | None = None
        self._pending_gate: tuple[str, list[Any]] | None = None
        self._input_buffer: Buffer | None
        if self.live:
            from prompt_toolkit.buffer import Buffer
            self._input_buffer = Buffer(multiline=False,
                                        accept_handler=self._on_accept)
        else:
            self._input_buffer = None
        # Set in build_application(); declared here so mypy tracks the type.
        self._narrative_window: Any = None
        self._input_window: Any = None
        self.application: Application[Any] | None = None
        # Set by run_live_tui._main so on_event / ensure_future can reference it.
        self._loop: asyncio.AbstractEventLoop | None = None
        self._rebuild()
        self.cursor_line = max(0, len(self._lines) - 1)   # log mode starts at tail

    # -- line rebuild ---------------------------------------------------------
    def _rebuild(self) -> None:
        self._lines = render_tree_to_lines(
            self.tree, self.fold_state, self._width, color=True, memo=self.memo)

    def set_width(self, width: int) -> None:
        if width != self._width:
            self._width = width
            node = self._cursor_node()
            self._rebuild()
            if node is not None:
                self.cursor_line = remap_cursor_to_node(
                    self._lines, node, fallback=self.cursor_line)
            else:
                self.cursor_line = max(0, min(self.cursor_line, len(self._lines) - 1))

    def _cursor_node(self) -> Any:
        if 0 <= self.cursor_line < len(self._lines):
            return self._lines[self.cursor_line].node
        return None

    # -- mouse support --------------------------------------------------------
    def _on_fold_click(self, fold: Any) -> None:
        self.toggle_fold_node(fold)

    def _make_mouse_handler(self, fold: Any) -> Any:
        from prompt_toolkit.mouse_events import MouseEventType

        def handler(mouse_event: Any) -> None:
            if mouse_event.event_type == MouseEventType.MOUSE_UP:
                self._on_fold_click(fold)
        return handler

    # -- formatted text for the control --------------------------------------
    def _get_formatted_text(self) -> list[Any]:
        frags: list[Any] = []
        for i, ln in enumerate(self._lines):
            style_prefix = "reverse " if i == self.cursor_line else ""
            handler = (self._make_mouse_handler(ln.node)
                       if (self.mouse and ln.is_fold_header
                           and getattr(ln.node, "collapsible", False)) else None)
            for s, t in ln.fragments:
                style = f"{style_prefix}{s}".strip()
                if handler is not None:
                    frags.append((style, t, handler))
                else:
                    frags.append((style, t))
            frags.append(("", "\n"))
        if frags and frags[-1] == ("", "\n"):
            frags.pop()
        return frags

    def _cursor_position(self) -> Any:
        from prompt_toolkit.data_structures import Point
        return Point(x=0, y=self.cursor_line)

    # -- navigation (called by key bindings; tested directly) ----------------
    _page_size = 20

    def _invalidate(self) -> None:
        if self.application is not None:
            self.application.invalidate()

    def move_cursor(self, delta: int) -> None:
        self.cursor_line = max(0, min(len(self._lines) - 1, self.cursor_line + delta))
        self.follow_tail = is_at_tail(self.cursor_line, len(self._lines))
        self._invalidate()

    def page(self, direction: int) -> None:
        self.move_cursor(direction * self._page_size)

    def goto_top(self) -> None:
        self.cursor_line = 0
        self.follow_tail = False
        self._invalidate()

    def goto_tail(self) -> None:
        self.cursor_line = max(0, len(self._lines) - 1)
        self.follow_tail = True
        self._invalidate()

    def next_fold(self) -> None:
        self.cursor_line = next_fold_line(self._lines, self.cursor_line)
        self.follow_tail = is_at_tail(self.cursor_line, len(self._lines))
        self._invalidate()

    def prev_fold(self) -> None:
        self.cursor_line = prev_fold_line(self._lines, self.cursor_line)
        self.follow_tail = False
        self._invalidate()

    def _enclosing_fold(self, node: Any) -> Any:
        return node if getattr(node, "collapsible", False) else None

    def toggle_fold_at_cursor(self) -> None:
        node = self._cursor_node()
        fold = self._enclosing_fold(node)
        if fold is None:
            return
        self.fold_state.toggle(fold)
        self._rebuild()
        # Fold-stable: keep the cursor pinned to the fold header (design §7.3).
        self.cursor_line = remap_cursor_to_node(self._lines, fold, fallback=self.cursor_line)
        self.follow_tail = is_at_tail(self.cursor_line, len(self._lines))
        self._invalidate()

    def toggle_fold_node(self, fold: Any) -> None:
        """Toggle a specific fold (mouse click target)."""
        if not getattr(fold, "collapsible", False):
            return
        self.fold_state.toggle(fold)
        self._rebuild()
        self.cursor_line = remap_cursor_to_node(self._lines, fold, fallback=self.cursor_line)
        self._invalidate()

    def expand_all(self) -> None:
        node = self._cursor_node()
        self.fold_state.set_all(True)
        self._rebuild()
        if node is not None:
            self.cursor_line = remap_cursor_to_node(self._lines, node, fallback=self.cursor_line)
        self._invalidate()

    def collapse_all(self) -> None:
        node = self._cursor_node()
        self.fold_state.set_all(False)
        self._rebuild()
        if node is not None:
            self.cursor_line = remap_cursor_to_node(self._lines, node, fallback=self.cursor_line)
        self._invalidate()

    def toggle_expand_all(self) -> None:
        if self.fold_state.default_expanded:
            self.collapse_all()
        else:
            self.expand_all()

    def copy_focused_node(self) -> None:
        node = self._cursor_node()
        text = node_copy_text(node) if node is not None else ""
        if not text:
            self.status_note = "nothing to copy"
            self._invalidate()
            return
        seq = osc52_sequence(text)
        writer = self._osc_writer or self._default_osc_writer
        try:
            writer(seq)
            self.status_note = f"copied {len(text)} chars (OSC 52)"
        except Exception:
            self.status_note = "clipboard unavailable"
        self._invalidate()

    def _default_osc_writer(self, seq: str) -> None:
        import sys
        sys.stdout.write(seq)
        sys.stdout.flush()

    # -- live input (design §7.5) --------------------------------------------
    def request_line(self, prompt_text: str = "> ") -> asyncio.Future[str]:
        """Return an awaitable that resolves when the user submits a line (or
        ESC → ESC_SENTINEL). Engine callbacks await this."""
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        self._active_input = fut
        self._input_prompt = prompt_text
        if self.application is not None:
            with contextlib.suppress(Exception):
                self.application.layout.focus(self._input_window)
        self._invalidate()
        return fut

    def user_chat_input_fn(self, _question: str) -> asyncio.Future[str]:
        return self.request_line("> ")

    def request_gate_choice(self, prompt_text: str, options: list[Any]) -> asyncio.Future[str]:
        """Return an awaitable that resolves with the chosen option label when
        the user selects a gate option via _select_gate_option."""
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        self._gate_future = fut
        self._gate_options = options
        self._gate_active = True
        self._input_prompt = prompt_text
        self._invalidate()
        return fut

    def _select_gate_option(self, index: int) -> None:
        if not (0 <= index < len(self._gate_options)):
            return
        label = self._gate_options[index].get("label", str(index))
        fut, self._gate_future = self._gate_future, None
        self._gate_active = False
        self._gate_options = []
        if fut is not None and not fut.done():
            fut.set_result(label)            # exact label — _match_human_option gives priority
        self._refocus_narrative()
        self._invalidate()

    def human_input_fn(self) -> asyncio.Future[str]:
        # Live gate dispatch: on_event stashes the pending gate from the
        # human_gate_pending event the executor emits just before this call.
        if self._pending_gate is not None:
            prompt, options = self._pending_gate
            self._pending_gate = None
            return self.request_gate_choice(prompt, options)
        return self.request_line("> ")

    def human_output_fn(self, text: str) -> None:
        # run_human_gate (gates.py:91-103) writes the gate banner/prompt/options
        # AND its validation feedback through output_fn. The prompt/options are
        # already shown as the GateNode + inline selector, so drop them; surface
        # the validation/retry parentheticals ("(no match…)", "(ambiguous…)") in
        # the footer so a rejected entry isn't invisible.
        t = text.strip()
        if t.startswith("(") and t.endswith(")"):
            self.status_note = t[1:-1]
            self._invalidate()

    def _on_accept(self, buff: Any) -> bool:
        self._submit_input(buff.text)
        return False                        # clear the buffer

    def _submit_input(self, text: str) -> None:
        if self._input_buffer is not None:
            self._input_buffer.text = ""
        # When a human gate is active the awaited future is _gate_future, not
        # _active_input. The engine's _match_human_option accepts numeric indices
        # ("1") and label prefixes ("Merge") alike, so feed it the raw typed text;
        # a non-match makes run_human_gate re-prompt (request_gate_choice runs
        # again, re-arming the gate). Without this, typing a choice + Enter would
        # resolve nothing and the gate would hang.
        if self._gate_active:
            gfut, self._gate_future = self._gate_future, None
            self._gate_active = False
            self._gate_options = []
            if gfut is not None and not gfut.done():
                gfut.set_result(text)
            self._refocus_narrative()
            self._invalidate()
            return
        fut, self._active_input = self._active_input, None
        if fut is not None and not fut.done():
            fut.set_result(text)
        self._refocus_narrative()
        self._invalidate()

    def _escape_input(self, *, cancel_gate: bool = False) -> None:
        """Unwind any pending input awaiter.

        For the regular input future (_active_input) we always resolve with
        ESC_SENTINEL so the engine's input loop sees a non-match and re-prompts.

        For the gate future (_gate_future) the behaviour depends on the caller:
        - ESC key (cancel_gate=False): resolve with ESC_SENTINEL so
          _match_human_option sees no match and run_human_gate re-prompts.
        - pause/EOF teardown (cancel_gate=True): *cancel* the future so
          `await result` in run_human_gate raises CancelledError and the engine
          unwinds cleanly.  Feeding ESC_SENTINEL here would only cause a
          re-loop that creates a fresh gate future which also hangs.
        """
        from imp.cli.prompt import ESC_SENTINEL
        if self._input_buffer is not None:
            self._input_buffer.text = ""
        fut, self._active_input = self._active_input, None
        if fut is not None and not fut.done():
            fut.set_result(ESC_SENTINEL)
        # Unwind gate future if one is pending.
        gfut, self._gate_future = self._gate_future, None
        self._gate_active = False
        self._gate_options = []
        if gfut is not None and not gfut.done():
            if cancel_gate:
                gfut.cancel()
            else:
                gfut.set_result(ESC_SENTINEL)
        self._refocus_narrative()
        self._invalidate()

    def _input_buffer_text(self) -> str:
        return self._input_buffer.text if self._input_buffer is not None else ""

    def _refocus_narrative(self) -> None:
        if self.application is not None:
            with contextlib.suppress(Exception):
                self.application.layout.focus(self._narrative_window)

    # -- interrupt reconciliation (H2) ----------------------------------------
    def request_pause(self) -> None:
        """Keyboard Ctrl-C OR an OS signal converge here (H2 c). Guarded so a
        signal + keystroke can't double-pause."""
        if self._pausing:
            return
        self._pausing = True
        self.exit_reason = "pause"
        # Unwind any awaited input future so run_change doesn't hang mid-prompt.
        # cancel_gate=True: cancel (not ESC_SENTINEL) the gate future so
        # run_human_gate's `await result` raises CancelledError and the engine
        # unwinds instead of re-looping into a new gate future that also hangs.
        self._escape_input(cancel_gate=True)
        # Flip the coordinator's pause + fire the runner interrupt (async).
        # Use ensure_future (loop-owned task) rather than
        # application.create_background_task — prompt_toolkit cancels all
        # background tasks during run_async teardown
        # (cancel_and_wait_for_background_tasks), so a background task created
        # just before application.exit() would be cancelled before it runs.
        # Loop-owned tasks are NOT cancelled by the application teardown.
        if self.coordinator is not None:
            import asyncio
            _task = asyncio.ensure_future(self.coordinator.signal_pause())
            _task  # noqa: B018  loop-owned task; must NOT be a background task
        if self.application is not None:
            self.application.exit()

    def request_exit_eof(self) -> None:
        """Keyboard Ctrl-D (H2 a, EOF path)."""
        if self._pausing:
            return
        self._pausing = True
        self.exit_reason = "eof"
        self._escape_input(cancel_gate=True)
        if self.application is not None:
            self.application.exit()

    def signal_paused_externally(self) -> None:
        """Called by the CLI when run_signal_receiver fires (H2 b) so the app
        also tears down. Converges on the same guard (H2 c)."""
        self.request_pause()

    # -- live event sink (design §7.7) ----------------------------------------
    def on_event(self, event: dict[str, Any]) -> None:
        """Synchronous on_event sink (design §7.7): builder.add then invalidate.
        No awaiting in the hot path. MUST run on the loop thread (see comment in
        __init__ — verified on-loop as of 2026-05-28)."""
        if self._builder is not None:
            self._builder.add(event)
            self.tree = self._builder.tree
        was_following = self.follow_tail
        self._rebuild()
        if was_following:
            self.cursor_line = max(0, len(self._lines) - 1)
            self.new_below_count = 0
        else:
            self.new_below_count += 1
            self.cursor_line = min(self.cursor_line, max(0, len(self._lines) - 1))
        self._invalidate()
        # Stash pending gate info so human_input_fn can route to the selector
        # (outside the builder block — reads the event directly, design §7.5 / Task 31)
        if event.get("type") == "human_gate_pending":
            p: dict[str, Any] = event.get("payload") or {}
            self._pending_gate = (str(p.get("prompt", "")), list(p.get("options") or []))

    async def clock_tick(self) -> None:
        """~1s tick so the footer's elapsed timer advances between events
        (design §7.7). Runs in the engine task group."""
        import anyio
        while True:
            await anyio.sleep(1.0)
            self._invalidate()

    # -- footer ---------------------------------------------------------------
    def _fmt_tokens(self) -> str:
        def h(n: int) -> str:
            return f"{n/1000:.1f}k" if n >= 1000 else str(n)
        t = self.tree.tokens
        return f"↑{h(t.input)} ↓{h(t.output)}"

    def _key_hints(self) -> str:
        if self.live:
            return "PgUp/Dn ⌃N/⌃P ⌃␣ ⌃E ⌃Y ⌃C q"
        return "↑↓ ⏎ a y Tab q"

    def _footer_text(self) -> list[tuple[str, str]]:
        root = self.tree.root
        ident = f"#{root.change_id if root.change_id is not None else '?'} {root.title}".strip()
        left = f"{ident} · {root.status} · {self._fmt_tokens()}"
        if self.status_note:
            left += f" · {self.status_note}"
        right = self._key_hints()
        return [("reverse", f" {left} "), ("", "  "), ("reverse", f" {right} ")]

    # -- application construction --------------------------------------------
    def build_application(self, *, _input: Any = None, _output: Any = None) -> Application[Any]:
        from prompt_toolkit.application import Application
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout import Layout
        from prompt_toolkit.layout.containers import HSplit, Window
        from prompt_toolkit.layout.controls import FormattedTextControl

        control = FormattedTextControl(
            text=self._get_formatted_text, focusable=True, show_cursor=False,
            get_cursor_position=self._cursor_position)
        self._narrative_window = Window(content=control, wrap_lines=True,
                                        always_hide_cursor=True)
        kb = KeyBindings()
        if not self.live:
            @kb.add("up")
            def _(event: Any) -> None: self.move_cursor(-1)

            @kb.add("down")
            def _(event: Any) -> None: self.move_cursor(1)

            @kb.add("pageup")
            def _(event: Any) -> None: self.page(-1)

            @kb.add("pagedown")
            def _(event: Any) -> None: self.page(1)

            @kb.add("g")
            @kb.add("home")
            def _(event: Any) -> None: self.goto_top()

            @kb.add("G")
            @kb.add("end")
            def _(event: Any) -> None: self.goto_tail()

            @kb.add("tab")
            def _(event: Any) -> None: self.next_fold()

            @kb.add("s-tab")
            def _(event: Any) -> None: self.prev_fold()

            @kb.add("enter")
            @kb.add("space")
            def _(event: Any) -> None: self.toggle_fold_at_cursor()

            @kb.add("a")
            def _(event: Any) -> None: self.toggle_expand_all()

            @kb.add("y")
            def _(event: Any) -> None: self.copy_focused_node()      # Task 26

            @kb.add("q")
            def _(event: Any) -> None: event.app.exit()

        from prompt_toolkit.layout.containers import VSplit
        from prompt_toolkit.layout.controls import BufferControl
        from prompt_toolkit.layout.controls import FormattedTextControl as _FTC

        windows: list[Any] = [self._narrative_window]
        if self.live:
            # NOTE: do NOT capture the event loop here — build_application runs
            # before anyio.run() starts, so the running loop doesn't exist yet.
            # run_live_tui sets self._loop inside _main (Task 32).
            prompt_ctrl = _FTC(text=lambda: [("bold", self._input_prompt)])
            assert self._input_buffer is not None  # set in __init__ when live=True
            self._input_window = Window(
                content=BufferControl(buffer=self._input_buffer), height=1)
            input_row = VSplit([Window(content=prompt_ctrl, width=2), self._input_window])
            windows.append(input_row)
        footer_window = Window(content=_FTC(text=self._footer_text), height=1)
        windows.append(footer_window)
        root_container = HSplit(windows)

        if self.live:
            # Input-bar bindings (the buffer handles normal typing itself).
            @kb.add("escape", eager=True)
            def _(event: Any) -> None: self._escape_input()

            @kb.add("c-c")
            def _(event: Any) -> None: self.request_pause()        # H2 a

            @kb.add("c-d")
            def _(event: Any) -> None: self.request_exit_eof()     # H2 a

            # Narrative nav via Ctrl-chords (letters/space/tab type — §7.4).
            @kb.add("pageup")
            def _(event: Any) -> None: self.page(-1)

            @kb.add("pagedown")
            def _(event: Any) -> None: self.page(1)

            @kb.add("end")
            def _(event: Any) -> None: self.goto_tail()            # re-attach to live tail

            @kb.add("home")
            def _(event: Any) -> None: self.goto_top()

            @kb.add("c-n")
            def _(event: Any) -> None: self.next_fold()

            @kb.add("c-p")
            def _(event: Any) -> None: self.prev_fold()

            @kb.add("c-space")
            def _(event: Any) -> None: self.toggle_fold_at_cursor()

            @kb.add("c-e")
            def _(event: Any) -> None: self.toggle_expand_all()

            @kb.add("c-y")
            def _(event: Any) -> None: self.copy_focused_node()

        focused = self._input_window if self.live else self._narrative_window
        self.application = Application(
            layout=Layout(root_container, focused_element=focused),
            key_bindings=kb, full_screen=True, mouse_support=self.mouse,
            input=_input, output=_output)
        return self.application


def osc52_sequence(text: str) -> str:
    """Return an OSC 52 escape sequence that copies *text* to the system clipboard.

    The sequence is: ESC ] 52 ; c ; <base64> BEL  (design §7.6 / §13).
    TODO §13: for terminals that reject oversized sequences, consider chunking or
    truncating with notice."""
    import base64
    b64 = base64.b64encode(text.encode("utf-8", "replace")).decode("ascii")
    return f"\x1b]52;c;{b64}\x07"


def node_copy_text(node: Any) -> str:
    """The FULL underlying text of a node (design §7.6) — the whole message /
    tool output, not the on-screen slice."""
    if isinstance(node, nv.AgentTextNode):
        return node.text
    if isinstance(node, nv.UserMessageNode):
        return node.text
    if isinstance(node, nv.ThinkingFold):
        return node.content
    if isinstance(node, nv.StepContextNode):
        return str(node.payload)
    if isinstance(node, nv.ToolBurstFold):
        parts = []
        for c in node.calls:
            args = " ".join(f"{k}={v}" for k, v in (c.input or {}).items())
            parts.append(f"{c.name} {args}\n{c.result or ''}".rstrip())
        return "\n\n".join(parts)
    if isinstance(node, nv.SubagentFold):
        return "\n".join(node_copy_text(c) for c in node.tree.root.children
                         if isinstance(c, (nv.AgentTextNode, nv.UserMessageNode,
                                           nv.ThinkingFold)))
    if isinstance(node, nv.ErrorNode):
        return node.message
    if isinstance(node, nv.OutcomeLine):
        return pal.action_text(node.action)
    return ""


def run_log_tui(tree: nv.NarrativeTree, *, full: bool, mouse: bool,
                _input: Any = None, _output: Any = None) -> int:
    """Module-level entry point for log-mode TUI (called by _cmd_log)."""
    app_obj = NarrativeApp(tree, live=False, mouse=mouse, full=full)
    application = app_obj.build_application(_input=_input, _output=_output)
    application.run()
    return 0


def run_live_tui(*, run_change: Any, coordinator: Any, mouse: bool,
                 seed_events: Any = None, _input: Any = None, _output: Any = None) -> Any:
    """Run the live narrative TUI concurrently with the engine on one asyncio
    loop (design §7.7). ``run_change`` is a coroutine callable that already has
    change bound (e.g. via functools.partial) EXCEPT for the keyword callbacks
    ``on_event``, ``human_input_fn``, ``human_output_fn``,
    ``user_chat_input_fn``, and ``coordinator``, which this function supplies.

    ``seed_events`` (optional, F4): a list of prior events to feed into the
    builder synchronously *before* anyio.run() starts.  This completes before
    _engine is started, so the read→subscribe window is empty and no event is
    dropped or double-applied.  Used by ``imp resume`` to show the full prior
    log as scrollable history before the live tail begins.

    Returns the RunResult (or None if the app exited without an engine result)."""
    import anyio

    builder = nv.NarrativeBuilder()
    # F4: seed the ENTIRE prior log synchronously, before anything async runs.
    # This completes before _engine is started, so the read→subscribe window is
    # empty and no event is dropped or double-applied.
    if seed_events is not None:
        for e in seed_events:
            builder.add(e)
    app_obj = NarrativeApp(builder.tree, live=True, mouse=mouse,
                           builder=builder, coordinator=coordinator)
    application = app_obj.build_application(_input=_input, _output=_output)

    result_box: dict[str, Any] = {}

    async def _main() -> None:
        # Store the running loop so on_event (and ensure_future) can reference it.
        app_obj._loop = asyncio.get_running_loop()
        async with anyio.create_task_group() as tg:
            # Start run_signal_receiver if coordinator provides it (guard allows
            # test doubles that omit the method).
            if hasattr(coordinator, "run_signal_receiver"):
                tg.start_soon(coordinator.run_signal_receiver)
            # Clock tick keeps the footer's elapsed timer alive (H1: carry-forward).
            tg.start_soon(app_obj.clock_tick)

            async def _engine() -> None:
                try:
                    result_box["result"] = await run_change(
                        on_event=app_obj.on_event,
                        human_input_fn=app_obj.human_input_fn,
                        human_output_fn=app_obj.human_output_fn,
                        user_chat_input_fn=app_obj.user_chat_input_fn,
                        coordinator=coordinator,
                    )
                finally:
                    # Engine finished (completed, paused, or errored) — ensure
                    # exit_reason is set so the CLI can act on it, then exit the app.
                    # Guard: if Ctrl-C/Ctrl-D already called application.exit() and
                    # the app's internal future is cleared, a second exit() raises
                    # "Application is not running". Swallow it so the interrupt path
                    # doesn't surface an ExceptionGroup traceback.
                    app_obj.exit_reason = app_obj.exit_reason or "done"
                    # Swallow "Application is not running" (Ctrl-C/Ctrl-D path).
                    with contextlib.suppress(Exception):
                        application.exit()

            tg.start_soon(_engine)

            # H2 e: handle_sigint=False so prompt_toolkit doesn't intercept
            # SIGINT — run_signal_receiver owns it.
            await application.run_async(handle_sigint=False)
            # App exited (engine done, Ctrl-C, or Ctrl-D) — cancel remaining tasks.
            tg.cancel_scope.cancel()

    anyio.run(_main)
    return result_box.get("result")


def _render_prose_ansi(node: Any, avail_width: int, color: bool) -> list[str]:
    """Render an AgentTextNode/UserMessageNode to a list of ANSI line strings.

    AgentTextNode → markdown (bold/italic/lists/headers + syntax-highlighted
    fenced code). UserMessageNode → plain '> '-prefixed wrapped text. A rich
    failure on any chunk degrades to plain wrapped text (design §7.2, never
    crash the frame)."""
    import textwrap
    avail = max(8, avail_width)
    if isinstance(node, nv.UserMessageNode):
        return [f"> {ln}" for ln in (textwrap.wrap(node.text, width=avail) or [""])]
    text = node.text
    if not color:
        out: list[str] = []
        for para in text.split("\n"):
            out.extend(textwrap.wrap(para, width=avail) or [""])
        return out
    try:
        from io import StringIO

        from rich.console import Console
        from rich.markdown import Markdown
        buf = StringIO()
        console = Console(file=buf, width=avail, force_terminal=True,
                          color_system="standard", soft_wrap=False)
        console.print(Markdown(text))
        return buf.getvalue().rstrip("\n").split("\n")
    except Exception:
        out = []
        for para in text.split("\n"):
            out.extend(textwrap.wrap(para, width=avail) or [""])
        return out
