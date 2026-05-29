# src/imp/cli/render_text.py
"""Plain-text narrative view (--no-tui / non-TTY). Walks a NarrativeTree and
yields wrapped ANSI lines. No rich — plain wrapped text only (design §3, §6).

Default: folds render as one-line summaries; agent/user prose in full.
--full: every fold expanded inline (Task 13). Same content the TUI shows after
expand-all.
"""
from __future__ import annotations

import textwrap
from collections.abc import Iterator
from typing import Any

from imp.cli import narrative as nv
from imp.cli import palette as pal

_INDENT = "  "          # two spaces per tree level


def render_tree(tree: nv.NarrativeTree, *, full: bool, color: bool,
                width: int = 80) -> Iterator[str]:
    root = tree.root
    head = f"#{root.change_id if root.change_id is not None else '?'}  {root.title}".rstrip()
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
    """The single header line for a container node (PhaseNode / StepNode), with
    no children. Shared by `_render` and the live driver so indentation/label/
    color stay identical in both paths."""
    if isinstance(node, nv.PhaseNode):
        return _line(node.name, depth, color, *pal.NODE_COLORS["phase"])
    label = f"agent_gate: {node.name}" if node.kind == "agent_gate" else node.name
    if node.interactive:
        label += " (interactive)"
    return _line(label, depth, color, *pal.NODE_COLORS["step"])


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

    if isinstance(node, nv.UserMessageNode):
        for line in _wrap(f"> {node.text}", width, depth):
            yield pal.ansi(line, *pal.NODE_COLORS["user"], enabled=color)
        return

    if isinstance(node, nv.ThinkingFold):
        if full:
            yield _line(f"{pal.fold_glyph(expanded=True)} thinking", depth, color,
                        *pal.NODE_COLORS["thinking"])
            for line in _wrap(node.content, width, depth + 1):
                yield pal.ansi(line, *pal.NODE_COLORS["thinking"], enabled=color)
        else:
            summary = f"{pal.fold_glyph(expanded=False)} thinking · {len(node.content)} chars"
            yield _line(summary, depth, color, *pal.NODE_COLORS["thinking"])
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

    if isinstance(node, nv.ErrorNode):
        if node.is_warning:
            yield _line(f"warning: {node.message}", depth, color, *pal.NODE_COLORS["warning"])
        else:
            tag = f"[{node.category}] " if node.category else ""
            yield _line(f"error: {tag}{node.message}", depth, color, *pal.NODE_COLORS["error"])
        return

    if isinstance(node, nv.FallbackLine):
        kv = " ".join(f"{k}={v}" for k, v in node.payload.items())
        yield _line(f"{node.event_type}  {kv}".rstrip(), depth, color, "dim")
        return


def _burst_counts(burst: nv.ToolBurstFold) -> str:
    counts: dict[str, int] = {}
    for c in burst.calls:
        counts[c.name] = counts.get(c.name, 0) + 1
    by_count = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return " · ".join(f"{n} {name}" for name, n in by_count)


def _render_burst(burst: nv.ToolBurstFold, *, depth: int, full: bool,
                  color: bool, width: int) -> Iterator[str]:
    total = len(burst.calls)
    if not full:
        glyph = pal.fold_glyph(expanded=False)
        yield _line(f"{glyph} {total} tools · {_burst_counts(burst)}", depth, color,
                    *pal.NODE_COLORS["tools"])
        return
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


def _render_gate(gate: nv.GateNode, *, depth: int, color: bool, width: int) -> Iterator[str]:
    action = pal.action_text(gate.action) if gate.action is not None else "pending"
    pal.accent_color(gate.action) if gate.action is not None else None
    header = f"{gate.kind} gate → {action}"
    yield _line(header, depth, color, *pal.NODE_COLORS["gate"])
    if gate.prompt:
        for line in _wrap(gate.prompt, width, depth + 1):
            yield pal.ansi(line, *pal.NODE_COLORS["gate"], enabled=color)
        for opt in gate.options:
            yield _line(f"- {opt.get('label', opt)}", depth + 1, color,
                        *pal.NODE_COLORS["gate"])


def stream_event(event: Any, *, color: bool, width: int = 80) -> str | None:
    """Format ONE event as an append-only block for the --no-tui live stream.

    Returns the text (possibly multi-line) or None to skip. Subagent (sub:)
    events are indented one level. Deliberately fold-free: this is the streaming
    CI view, not the foldable tree (use `imp log` for that)."""
    if not isinstance(event, dict):
        return None
    etype = event.get("type")
    p = event.get("payload") or {}
    agent_id = event.get("agent_id")
    pad = _INDENT if isinstance(agent_id, str) and agent_id.startswith("sub:") else ""

    def styled(text: str, *codes: str) -> str:
        return "\n".join(pal.ansi(f"{pad}{ln}", *codes, enabled=color)
                         for ln in text.split("\n"))

    if etype == "phase_started":
        return styled(f"═══ {p.get('phase', '?')} ═══", *pal.NODE_COLORS["phase"])
    if etype == "phase_completed":
        return styled(f"═══ {p.get('phase', '?')} done ═══", *pal.NODE_COLORS["phase"])
    if etype == "step_started":
        if p.get("kind") == "agent_gate":
            return styled(f"── agent_gate: {p.get('gate_stem', '?')} ──",
                          *pal.NODE_COLORS["gate"])
        return styled(f"── {p.get('step', '?')} ──", *pal.NODE_COLORS["step"])
    if etype == "step_completed":
        ac = pal.accent_color(p.get("action"))
        return styled(f"→ {pal.action_text(p.get('action'))}", *((ac,) if ac else ()))
    if etype == "llm_response":
        from imp.cli.render_helpers import render_llm_response
        r = render_llm_response(p)
        text = "\n\n".join(t.strip() for t in r.texts if t.strip())
        return styled(text) if text else None
    if etype == "user_message":
        return styled(f"> {_stream_user_text(p)}", *pal.NODE_COLORS["user"])
    if etype == "thinking":
        c = str(p.get("content", ""))
        return styled(f"thinking · {len(c)} chars", *pal.NODE_COLORS["thinking"])
    if etype == "tool_call":
        args = " ".join(f"{k}={v}" for k, v in (p.get("input") or {}).items())
        return styled(f"⏺ {p.get('name', 'tool')}  {args}".rstrip(), *pal.NODE_COLORS["tools"])
    if etype == "tool_result":
        body = (p.get("content") or "")
        body = body if isinstance(body, str) else str(body)
        body = body.replace("\n", " ")
        if len(body) > 200:
            body = body[:200] + "…"
        err = " [error]" if p.get("error") else ""
        return styled(f"  → {body}{err}", *pal.NODE_COLORS["tools"])
    if etype == "tool_killed":
        return styled(f"  [killed] {p.get('name', 'tool')}", *pal.NODE_COLORS["error"])
    if etype in ("gate_evaluated", "human_gate_pending"):
        if etype == "human_gate_pending":
            return styled(f"gate: {p.get('prompt', '')}", *pal.NODE_COLORS["gate"])
        return styled(f"gate ({p.get('kind', '?')}) → {pal.action_text(p.get('action'))}",
                      *pal.NODE_COLORS["gate"])
    if etype == "error":
        tag = f"[{p.get('category')}] " if p.get("category") else ""
        return styled(f"error: {tag}{p.get('message', '?')}", *pal.NODE_COLORS["error"])
    if etype == "runner_warning":
        return styled(f"warning: {p.get('message', '?')}", *pal.NODE_COLORS["warning"])
    if etype in ("workflow_paused", "workflow_resumed"):
        return styled(etype.replace("_", " "), *pal.NODE_COLORS["metadata"])
    if etype == "agent_output":
        return None
    if etype == "change_created":
        return styled(f"#{p.get('change_id', '?')}  {p.get('title', '')}",
                      *pal.NODE_COLORS["workflow"])
    return None


def _stream_user_text(p: dict[str, Any]) -> str:
    return "".join(b.get("text", "") for b in (p.get("content") or [])
                   if isinstance(b, dict) and b.get("type") == "text")
