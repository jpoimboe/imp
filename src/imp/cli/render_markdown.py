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
    # Additional node branches are appended by later tasks, before this return.
    return


def render_markdown(tree: nv.NarrativeTree, *, full: bool) -> str:
    """Render a NarrativeTree to a Markdown document string."""
    blocks = [b for b in _walk(tree.root, full=full, level=1) if b]
    return "\n\n".join(blocks).strip() + "\n"
