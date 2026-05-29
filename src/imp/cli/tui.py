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
    """Prose (AgentTextNode/UserMessageNode) → fragments. Task 21 swaps the body
    for rich markdown; for now (Task 20) it is plain wrapped text so the line
    layer is testable without rich."""
    text = node.text if isinstance(node, nv.AgentTextNode) else f"> {node.text}"
    codes = () if isinstance(node, nv.AgentTextNode) else pal.NODE_COLORS["user"]
    return _wrap_fragments(text, width, depth, color, codes)
