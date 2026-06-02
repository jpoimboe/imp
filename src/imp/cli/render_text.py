# src/imp/cli/render_text.py
"""Plain-text narrative view (imp log / non-TTY). Walks a NarrativeTree and
yields wrapped ANSI lines. No rich — plain wrapped text only (design §3, §6).

Default: folds render as one-line summaries; agent/user prose in full.
--full: every fold expanded inline (Task 13). Same content the TUI shows after
expand-all.
"""
from __future__ import annotations

import json
import textwrap
from collections.abc import Iterator
from typing import Any

from imp.cli import narrative as nv
from imp.cli import palette as pal

_INDENT = "  "          # two spaces per tree level

_DIFFER_LABELS = {1: "vendor jump", 2: "same-vendor fallback", 3: "identical"}


def render_tree(tree: nv.NarrativeTree, *, full: bool, color: bool,
                width: int = 80) -> Iterator[str]:
    root = tree.root
    head = f"#{root.slug if root.slug is not None else '?'}  {root.title}".rstrip()
    if root.status != "running":
        head = f"{head}  ({root.status})"
    yield pal.ansi(head, *pal.NODE_COLORS["workflow"], enabled=color)
    for child in root.children:
        yield from _render(child, depth=0, full=full, color=color, width=width)


def _wrap(text: str, width: int, depth: int) -> list[str]:
    indent = _INDENT * depth
    avail = max(8, width - len(indent))
    out: list[str] = []
    for para in text.split("\n"):
        if not para:
            out.append(indent)
            continue
        for line in textwrap.wrap(para, width=avail) or [""]:
            out.append(f"{indent}{line}")
    return out


def _line(text: str, depth: int, color: bool, *codes: str) -> str:
    return pal.ansi(f"{_INDENT * depth}{text}", *codes, enabled=color)


def header_line(node: Any, *, depth: int, color: bool) -> str:
    """The single header line for a container node (PhaseNode / StepNode /
    SubagentFold), with no children. Shared by `_render` and the live driver so
    indentation/label/color stay identical in both paths. For a SubagentFold this
    is the `task → {name}` boundary: subagent color, no fold glyph, and no
    (running)/(error) suffix — the live driver prints it while the subagent is
    still running, so any status suffix would be permanently wrong once it
    completes (completion is signaled by the outcome line instead)."""
    if isinstance(node, nv.PhaseNode):
        return _line(node.name, depth, color, *pal.NODE_COLORS["phase"])
    if isinstance(node, nv.SubagentFold):
        return _line(f"task → {node.subagent_name}", depth, color,
                     *pal.NODE_COLORS["subagent"])
    label = node.name
    if node.interactive:
        label += " (interactive)"
    return _line(label, depth, color, *pal.NODE_COLORS["step"])


def subagent_outcome_lines(sub: nv.SubagentFold, *, depth: int, color: bool,
                           width: int) -> Iterator[str]:
    """The subagent's structured return value, rendered readably at the passed
    `depth` (no internal +1 — the caller passes the prose depth). Empty when the
    subagent has no outcome yet (still running / force-popped).

    The outcome is `json.dumps(...)` of the structured output the parent consumed,
    so its prose newlines are escaped to literal `\\n`. We parse it back: a dict
    renders short scalar fields inline on one `→ key=value · …` line and each
    multi-line string field as a labeled block with REAL newlines (wrapped like
    agent prose). A non-dict (the error case — a failed dispatch returns a plain
    string) renders as prose. Error-styled when `sub.is_error`."""
    raw = sub.outcome or ""
    if not raw:
        return
    codes = pal.NODE_COLORS["error"] if sub.is_error else pal.NODE_COLORS["subagent"]
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        data = None
    if not isinstance(data, dict):
        for line in _wrap(raw, width, depth):
            yield pal.ansi(line, *codes, enabled=color)
        return
    scalars: list[str] = []
    blocks: list[tuple[str, str]] = []
    for k, v in data.items():
        if isinstance(v, str) and "\n" in v:
            blocks.append((k, v))
        else:
            scalars.append(f"{k}={v}")
    if scalars:
        yield _line("→ " + " · ".join(scalars), depth, color, *codes)
    for k, v in blocks:
        yield _line(f"{k}:", depth, color, *codes)
        for line in _wrap(v, width, depth + 1):
            yield pal.ansi(line, *codes, enabled=color)


def _render(node: Any, *, depth: int, full: bool, color: bool,
            width: int) -> Iterator[str]:
    if isinstance(node, nv.PhaseNode):
        yield header_line(node, depth=depth, color=color)
        for c in node.children:
            yield from _render(c, depth=depth + 1, full=full, color=color, width=width)
        return

    if isinstance(node, nv.StepNode):
        yield header_line(node, depth=depth, color=color)
        for c in node.children:
            yield from _render(c, depth=depth + 1, full=full, color=color, width=width)
        return

    if isinstance(node, nv.AgentTextNode):
        for line in _wrap(node.text, width, depth):
            yield pal.ansi(line, *pal.NODE_COLORS["agent"], enabled=color)
        return

    if isinstance(node, nv.LiteralTextNode):
        for line in _wrap(node.text, width, depth):
            yield pal.ansi(line, *pal.NODE_COLORS["agent"], enabled=color)
        return

    if isinstance(node, nv.UserMessageNode):
        for line in _wrap(f"> {node.text}", width, depth):
            yield pal.ansi(line, *pal.NODE_COLORS["user"], enabled=color)
        return

    if isinstance(node, nv.ThinkingFold):
        if not node.content.strip():
            return  # blank/whitespace thinking is pure noise — drop in both modes
        if full:
            yield _line(f"{pal.fold_glyph(expanded=True)} thinking", depth, color,
                        *pal.NODE_COLORS["thinking"])
            for line in _wrap(node.content, width, depth + 1):
                yield pal.ansi(line, *pal.NODE_COLORS["thinking"], enabled=color)
        # full=False: thinking is noise in the default view — render nothing.
        # (A transient `· thinking` status cue lives in narrative.current_activity.)
        return

    if isinstance(node, nv.ToolBurstFold):
        yield from _render_burst(node, depth=depth, full=full, color=color, width=width)
        return

    if isinstance(node, nv.SubagentFold):
        yield from _render_subagent(node, depth=depth, full=full, color=color, width=width)
        return

    if isinstance(node, nv.StepContextNode):
        yield from _render_step_context(node, depth=depth, full=full, color=color, width=width)
        return

    if isinstance(node, nv.GateNode):
        yield from _render_gate(node, depth=depth, color=color, width=width)
        return

    if isinstance(node, nv.OutcomeLine):
        text = pal.action_text(node.action)
        ac = pal.accent_color(node.action)
        codes = (ac,) if ac else ()
        yield _line(f"→ {text}", depth, color, *codes)
        return

    if isinstance(node, nv.BreadcrumbLine):
        parts = [f"✓ {node.step_name}"]
        if node.elapsed_s is not None:
            parts.append(f"{int(node.elapsed_s)}s")
        parts.extend(node.extras)
        verb_text = pal.action_text(node.action) if node.action is not None else None
        if verb_text and verb_text != "continue":
            parts.append(f"→ {verb_text}")
        ac = pal.accent_color(node.action)
        codes = (ac,) if ac else ()
        yield _line(" · ".join(parts), depth, color, *codes)
        return

    if isinstance(node, nv.ErrorNode):
        if node.is_warning:
            yield _line(f"warning: {node.message}", depth, color, *pal.NODE_COLORS["warning"])
        else:
            tag = f"[{node.category}] " if node.category else ""
            yield _line(f"error: {tag}{node.message}", depth, color, *pal.NODE_COLORS["error"])
        return

    if isinstance(node, nv.RolesResolvedNode):
        yield _line("roles resolved:", depth, color)
        col_w = max((len(r["role"]) for r in node.rows), default=0)  # role-name column
        for r in node.rows:
            # two-space sub-indent under the "roles resolved:" header
            line = f"  {r['role']:<{col_w}} → {r['provider']}/{r['model']}"
            if r["differ_tier"] is not None:
                label = _DIFFER_LABELS.get(r["differ_tier"], "?")
                line += (f"   (differ from {r['differ_from']}: "
                         f"tier {r['differ_tier']}, {label})")
            yield _line(line, depth, color)
        return

    if isinstance(node, nv.FallbackLine):
        kv = " ".join(f"{k}={v}" for k, v in node.payload.items())
        yield _line(f"{node.event_type}  {kv}".rstrip(), depth, color, "dim")
        return


def _render_burst(burst: nv.ToolBurstFold, *, depth: int, full: bool,
                  color: bool, width: int) -> Iterator[str]:
    if not full:
        return  # tool bursts are noise in the default view — render nothing.
    yield _line(f"{pal.fold_glyph(expanded=True)} tools", depth, color,
                *pal.NODE_COLORS["tools"])
    for c in burst.calls:
        args = " ".join(f"{k}={v}" for k, v in (c.input or {}).items())
        killed = " [killed]" if c.killed else ""
        result = (c.result or "").replace("\n", " ")
        if len(result) > 60:
            result = result[:60] + "…"
        line = f"⏺ {c.name}  {args}  → {result}{killed}".rstrip()
        for wl in _wrap(line, width, depth + 1):
            yield pal.ansi(wl, *pal.NODE_COLORS["tools"], enabled=color)


def _render_subagent(sub: nv.SubagentFold, *, depth: int, full: bool,
                     color: bool, width: int) -> Iterator[str]:
    if not full:
        glyph = pal.fold_glyph(expanded=False)
        tail = "running" if sub.running else f"→ {(sub.outcome or '').strip()[:40]}"
        yield _line(f"{glyph} task → {sub.subagent_name} · {sub.event_count} events · {tail}",
                    depth, color, *pal.NODE_COLORS["subagent"])
        return
    yield _line(f"{pal.fold_glyph(expanded=True)} task → {sub.subagent_name}",
                depth, color, *pal.NODE_COLORS["subagent"])
    for c in sub.tree.root.children:
        yield from _render(c, depth=depth + 1, full=full, color=color, width=width)


def _render_step_context(ctx: nv.StepContextNode, *, depth: int, full: bool,
                         color: bool, width: int) -> Iterator[str]:
    p = ctx.payload
    provider = p.get("provider", "?")
    model = p.get("model", "?")
    tools = p.get("tools_schema") or []
    if not full:
        yield _line(f"{pal.fold_glyph(expanded=False)} context · {provider}/{model} · "
                    f"{len(tools)} tools", depth, color, *pal.NODE_COLORS["metadata"])
        return
    yield _line(f"{pal.fold_glyph(expanded=True)} context · {provider}/{model} · "
                f"max_tool_calls={p.get('max_tool_calls', '?')}", depth, color,
                *pal.NODE_COLORS["metadata"])
    yield _line("system_prompt:", depth + 1, color, *pal.NODE_COLORS["metadata"])
    for line in _wrap(str(p.get("system_prompt", "")), width, depth + 2):
        yield pal.ansi(line, *pal.NODE_COLORS["metadata"], enabled=color)
    yield _line("tools:", depth + 1, color, *pal.NODE_COLORS["metadata"])
    for t in tools:
        name = t.get("name", t) if isinstance(t, dict) else t
        yield _line(f"- {name}", depth + 2, color, *pal.NODE_COLORS["metadata"])


def _render_gate(gate: nv.GateNode, *, depth: int, color: bool, width: int,
                 recap: bool = True) -> Iterator[str]:
    """Render a gate node. `recap=True` (imp log) emits header + prompt + every
    option — the permanent record. `recap=False` (live driver) emits the header
    line only, because run_human_gate already showed the prompt/options as live
    chrome; re-rendering them would duplicate a multi-line block in scroll-back."""
    action = pal.action_text(gate.action) if gate.action is not None else "pending"
    accent = pal.accent_color(gate.action) if gate.action is not None else None
    codes = (accent,) if accent else pal.NODE_COLORS["gate"]
    header = f"{gate.kind} gate → {action}"
    yield _line(header, depth, color, *codes)
    if recap and gate.prompt:
        for line in _wrap(gate.prompt, width, depth + 1):
            yield pal.ansi(line, *pal.NODE_COLORS["gate"], enabled=color)
        for opt in gate.options:
            yield _line(f"- {opt.get('label', opt)}", depth + 1, color,
                        *pal.NODE_COLORS["gate"])


