# src/imp/cli/tui.py
"""Interactive TUI: a prompt_toolkit Application rendering a NarrativeTree via
rich. rich + prompt_toolkit are confined to this module (design §7.2).

This file is layered:
  - FoldState / RenderedLine / render_tree_to_lines  — pure, testable
  - cursor + scroll helpers                          — pure, testable
  - the Application shell                             — thin, smoke-tested
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from imp.cli import narrative as nv
from imp.cli import palette as pal

# StyleAndTextTuples == list[tuple[str, str]]  (prompt_toolkit fragment list)
Fragments = list


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
        self._d: "OrderedDict[tuple[int, int], list[str]]" = OrderedDict()
        self.max_entries = max_entries
        self.hits = 0

    @property
    def size(self) -> int:
        return len(self._d)

    def get(self, key: tuple[int, int]) -> "list[str] | None":
        v = self._d.get(key)
        if v is not None:
            self.hits += 1
            self._d.move_to_end(key)
        return v

    def put(self, key: tuple[int, int], value: "list[str]") -> None:
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
                         memo: "AnsiMemo | None" = None) -> list[RenderedLine]:
    """Pure: NarrativeTree + fold state + width → ordered RenderedLines.

    `memo` (optional) caches per-(node,width) rich/ANSI output for prose nodes
    (design §7.2 / F5); pass None in tests that don't care about caching."""
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


def _line(out, text, depth, node, color, *codes, is_fold_header=False):
    s = pal.ansi(f"{_INDENT * depth}{text}", *codes, enabled=color)
    out.append(RenderedLine(fragments=_ansi_to_fragments(s), node=node,
                            is_fold_header=is_fold_header))


def _emit(out, node, *, depth, fs, width, color, memo) -> None:
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


def _emit_fold(out, node, *, depth, fs, width, color, memo) -> None:
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


def _wrap_fragments(text, width, depth, color, codes) -> list[Fragments]:
    import textwrap
    indent = _INDENT * depth
    avail = max(8, width - len(indent))
    out: list[Fragments] = []
    for para in text.split("\n"):
        for line in (textwrap.wrap(para, width=avail) or [""]):
            out.append(_ansi_to_fragments(pal.ansi(f"{indent}{line}", *codes, enabled=color)))
    return out


def _prose_lines(node, width, depth, color, memo) -> list[Fragments]:
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
                 full: bool = False) -> None:
        self.tree = tree
        self.live = live
        self.mouse = mouse
        self.fold_state = FoldState(default_expanded=full)
        self.memo = AnsiMemo()
        self.cursor_line = 0
        self.follow_tail = True               # sticky tail (live); harmless in log
        self._width = 80
        self._lines: list[RenderedLine] = []
        self._rebuild()
        self.cursor_line = max(0, len(self._lines) - 1)   # log mode starts at tail
        self.application = None               # set in build_application()

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

    def _cursor_node(self) -> Any:
        if 0 <= self.cursor_line < len(self._lines):
            return self._lines[self.cursor_line].node
        return None

    # -- formatted text for the control --------------------------------------
    def _get_formatted_text(self):
        frags = []
        for i, ln in enumerate(self._lines):
            style_prefix = "reverse " if i == self.cursor_line else ""
            for s, t in ln.fragments:
                frags.append((f"{style_prefix}{s}".strip(), t))
            frags.append(("", "\n"))
        if frags and frags[-1] == ("", "\n"):
            frags.pop()
        return frags

    def _cursor_position(self):
        from prompt_toolkit.layout.controls import Point
        return Point(x=0, y=self.cursor_line)

    # -- application construction (Task 24 adds key bindings) ----------------
    def build_application(self):
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

        @kb.add("q")
        def _quit(event) -> None:
            event.app.exit()

        root_container = HSplit([self._narrative_window])
        self.application = Application(
            layout=Layout(root_container, focused_element=self._narrative_window),
            key_bindings=kb, full_screen=True, mouse_support=self.mouse)
        return self.application


def _render_prose_ansi(node, avail_width: int, color: bool) -> "list[str]":
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
