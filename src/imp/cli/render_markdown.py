from __future__ import annotations

import html
from collections.abc import Iterator
from typing import Any

from imp.cli import narrative as nv
from imp.cli import render_text as rt

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
        yield _details(_md_escape(f"context · {provider}/{model}"),
                       "\n\n".join(inner_blocks), full=full)
        return
    # Additional node branches are appended by later tasks, before this return.
    return


def render_markdown(tree: nv.NarrativeTree, *, full: bool) -> str:
    """Render a NarrativeTree to a Markdown document string."""
    blocks = [b for b in _walk(tree.root, full=full, level=1) if b]
    return "\n\n".join(blocks).strip() + "\n"
