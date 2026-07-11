from __future__ import annotations

import html
import json
from collections.abc import Iterator
from typing import Any

from imp.cli import narrative as nv
from imp.cli import palette as pal
from imp.cli import render_text as rt
from imp.cli.render_summary import (
    StepRow,
    SummaryHeader,
    _dur_text,
    _fmt_cost,
    _fmt_tokens,
    _sub_body,
    _tok_total,
    header_totals,
)

_MAX_HEADING = 6


def _md_escape(text: str) -> str:
    """Neutralize untrusted content: caret-escape C0/DEL control bytes (reusing
    render_text's control-byte guard), then HTML-escape the three significant
    characters so a `<script>` in a tool result or agent turn renders as visible
    text, never a live tag."""
    return html.escape(rt._caret_escape(text), quote=False)


def _heading(level: int, text: str) -> str:
    return "#" * min(level, _MAX_HEADING) + " " + text.replace("\n", " ")


def _fence(text: str) -> str:
    """Wrap literal content in a code fence long enough to survive any backtick
    run inside it. Control bytes are caret-escaped; the fence itself guarantees
    the content is inert when later rendered to HTML."""
    body = rt._caret_escape(text)
    longest = 0
    run = 0
    for ch in body:
        run = run + 1 if ch == "`" else 0
        longest = max(longest, run)
    ticks = "`" * max(3, longest + 1)
    return f"{ticks}\n{body}\n{ticks}"


def _details(summary: str, inner: str, *, full: bool) -> str:
    """A fold: collapsed <details> in default mode, an inline bold header +
    content under --full. `summary` is imp's own label (safe); `inner` is a
    Markdown block already assembled from escaped content."""
    if full:
        return f"**{summary}**\n\n{inner}" if inner else f"**{summary}**"
    return f"<details>\n<summary>{summary}</summary>\n\n{inner}\n\n</details>"


def _tool_call_text(c: nv.ToolCall) -> str:
    lines = [f"· {nv.display_tool_name(c.name)}"]
    for k, v in (c.input or {}).items():
        lines.append(f"  {k}={' '.join(str(v).split())}")
    if c.result:
        lines.append("")
        lines.append(c.result.rstrip("\n"))
    return "\n".join(lines)


def _subagent_status_md(sub: nv.SubagentFold) -> str:
    label = sub.subagent_name
    model = rt.subagent_model(sub)
    if model:
        label += f" · {model}"
    if sub.running:
        label += " (running)"
    elif sub.terminal_state == "stalled":
        label += " (stalled)"
    elif sub.terminal_state == "interrupted":
        label += " (interrupted)"
    elif sub.terminal_state == "context_exhausted":
        label += " (context exhausted)"
    elif sub.is_error:
        label += " (failed)"
    elapsed = nv.elapsed_seconds(sub.start_ts, sub.end_ts)
    done = "" if sub.running else rt.fmt_seconds_done(elapsed)
    if done:
        label += f" · {done}"
    return f"· {_md_escape(label)}"


def _subagent_outcome_md(sub: nv.SubagentFold) -> str:
    raw = sub.outcome or ""
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        data = None
    if not isinstance(data, dict):
        return _md_escape(raw)
    scalars: list[str] = []
    blocks: list[tuple[str, str]] = []
    for k, v in data.items():
        if isinstance(v, str) and "\n" in v:
            blocks.append((k, v))
        else:
            scalars.append(f"{k}={v}")
    parts: list[str] = []
    if scalars:
        parts.append("→ " + _md_escape(" · ".join(scalars)))
    for k, v in blocks:
        parts.append(f"**{_md_escape(k)}:**\n\n{_md_escape(v)}")
    return "\n\n".join(parts)


def _walk_children(children: list[Any], *, full: bool, level: int) -> Iterator[str]:
    for child in children:
        yield from _walk(child, full=full, level=level)


def _walk(node: Any, *, full: bool, level: int) -> Iterator[str]:
    if isinstance(node, nv.WorkflowNode):
        yield _heading(level, _md_escape(node.title or "imp log"))
        yield from _walk_children(node.children, full=full, level=level + 1)
        return
    if isinstance(node, nv.PhaseNode):
        yield _heading(level, _md_escape(node.label or node.name))
        yield from _walk_children(node.children, full=full, level=level + 1)
        return
    if isinstance(node, nv.StepNode):
        yield _heading(level, _md_escape(node.label or node.name))
        yield from _walk_children(node.children, full=full, level=level + 1)
        return
    if isinstance(node, nv.AgentTextNode):
        text = node.text.strip()
        if text:
            yield _md_escape(text)
        return
    if isinstance(node, nv.LiteralTextNode):
        text = node.text.strip()
        if text:
            yield _md_escape(text)
        return
    if isinstance(node, nv.UserMessageNode):
        text = node.text.strip()
        if node.interactive and text:
            yield "\n".join("> " + line
                            for line in _md_escape(text).split("\n"))
        return
    if isinstance(node, nv.ThinkingFold):
        content = node.content.strip()
        if content:
            yield _details("thinking", _md_escape(content), full=full)
        return
    if isinstance(node, nv.ToolBurstFold):
        calls = nv.visible_calls(node)
        if not calls:
            return
        recap = rt._burst_summary(node) or "tools"
        inner = "\n\n".join(_fence(_tool_call_text(c)) for c in calls)
        yield _details(_md_escape(recap), inner, full=full)
        return
    if isinstance(node, nv.StepContextNode):
        p = node.payload
        provider = str(p.get("provider", "?"))
        model = str(p.get("model", "?"))
        parts = [f"provider: {provider}", f"model: {model}",
                 f"max_tool_calls: {p.get('max_tool_calls', '?')}"]
        inner_blocks = [_md_escape(" · ".join(parts))]
        system_prompt = str(p.get("system_prompt", ""))
        if system_prompt:
            inner_blocks.append("system_prompt:")
            inner_blocks.append(_fence(system_prompt))
        summary = f"Internal context (imp system prompts) — {provider}/{model}"
        yield _details(_md_escape(summary), "\n\n".join(inner_blocks), full=full)
        return
    if isinstance(node, nv.SubagentFold):
        if not full:
            yield _subagent_status_md(node)
            return
        yield f"**{_md_escape(node.subagent_name)}**"
        yield from _walk_children(node.tree.root.children, full=full, level=level + 1)
        outcome = _subagent_outcome_md(node)
        if outcome:
            yield outcome
        return
    if isinstance(node, nv.OutcomeLine):
        yield "→ " + _md_escape(pal.action_text(node.action))
        return
    if isinstance(node, nv.BreadcrumbLine):
        name = node.step_label or node.step_name
        if node.interrupted:
            parts = [f"{name} interrupted"]
        elif node.show_complete:
            parts = [f"{name} · complete"]
        else:
            parts = [name]
        done = rt.fmt_seconds_done(node.elapsed_s)
        if done:
            parts.append(done)
        yield "_" + _md_escape(" · ".join(parts)) + "_"
        return
    if isinstance(node, nv.SkillLoadNode):
        yield "loading skill " + _md_escape(node.name)
        return
    if isinstance(node, nv.ErrorNode):
        if node.is_warning:
            yield "> **warning:** " + _md_escape(node.message)
            return
        tag = f"[{node.category}] " if node.category else ""
        block = "> **error:** " + _md_escape(f"{tag}{node.message}")
        if node.detail and node.detail not in node.message:
            block += "\n>\n" + "\n".join(
                "> " + line for line in _md_escape(node.detail).split("\n"))
        yield block
        return
    if isinstance(node, nv.PauseNode):
        yield f"**paused ({_md_escape(node.reason)})**"
        return
    if isinstance(node, nv.RenameNode):
        yield 'Set name to "' + _md_escape(node.name) + '"'
        return
    if isinstance(node, nv.WarningsSummaryNode):
        noun = "warning" if node.total == 1 else "warnings"
        lines = [f"> **{node.total} {noun} this run**"]
        for message, count in node.lines:
            suffix = f" (×{count})" if count > 1 else ""
            lines.append("> - " + _md_escape(message) + suffix)
        yield "\n".join(lines)
        return
    if isinstance(node, nv.CompletedNode):
        yield f"**✓ Completed {_md_escape(node.quest_id)}.**"
        return
    if isinstance(node, nv.MergedNode):
        yield f"**✓ Merged {_md_escape(node.quest_id)} into {_md_escape(node.base)}.**"
        return
    if isinstance(node, nv.TiersResolvedNode):
        if not full:
            return
        yield "tiers resolved:"
        rows = "\n".join(
            f"- {_md_escape(str(r['tier']))} → "
            f"{_md_escape(str(r['provider']))}/{_md_escape(str(r['model']))}"
            for r in node.rows)
        if rows:
            yield rows
        return
    if isinstance(node, nv.FallbackLine):
        kv = " ".join(f"{k}={v}" for k, v in node.payload.items())
        yield _md_escape(f"{node.event_type} {kv}".rstrip())
        return
    # Additional node branches are appended by later tasks, before this return.
    return


def render_markdown(tree: nv.NarrativeTree, *, full: bool) -> str:
    """Render a NarrativeTree to a Markdown document string."""
    blocks = [b for b in _walk(tree.root, full=full, level=1) if b]
    return "\n\n".join(blocks).strip() + "\n"


def render_summary_markdown(summary: list[StepRow], *,
                            header: SummaryHeader) -> str:
    """Render the `imp log --summary` view as a Markdown document."""
    work, tokens, cost = header_totals(summary, header.total_seconds)
    open_label = header.status
    blocks = [
        _heading(1, _md_escape(f"{header.workflow} — {header.quest_id}")),
        (f"**Status:** {_md_escape(header.status)}"
         f" · **Total:** {_dur_text(work)}"
         f" · **Tokens:** {_fmt_tokens(tokens)}"
         f" · **Cost:** {_fmt_cost(cost)}"),
    ]
    grouped: list[tuple[str, list[str]]] = []
    for row in summary:
        if not grouped or grouped[-1][0] != row.phase:
            grouped.append((row.phase, []))
        step = _md_escape(row.step).replace("|", "\\|")
        grouped[-1][1].append(
            f"| {step} | {_dur_text(row.work_seconds, open_label)}"
            f" | {_fmt_tokens(_tok_total(row.tokens))} | {_fmt_cost(row.cost)} |")
        for sub in row.subagents:
            body = _md_escape(_sub_body(sub)).replace("|", "\\|")
            grouped[-1][1].append(
                f"| {body} | {_dur_text(sub.wall_seconds)}"
                f" | {_fmt_tokens(_tok_total(sub.tokens))} | {_fmt_cost(sub.cost)} |")
    for phase, rows in grouped:
        table = "\n".join([
            _heading(2, _md_escape(phase)),
            "| Step | Duration | Tokens | Cost |",
            "|---|---|---|---|",
            *rows,
        ])
        blocks.append(table)
    return "\n\n".join(blocks).strip() + "\n"
