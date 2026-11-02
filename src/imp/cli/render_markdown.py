# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
from __future__ import annotations

import html
import json
from collections.abc import Callable, Iterator
from typing import Any

from markdown_it import MarkdownIt
from markdown_it.token import Token

from imp.cli import narrative as nv
from imp.cli import palette as pal
from imp.cli import render_text as rt
from imp.cli.accounting import RowUsage
from imp.cli.quota_column import QuotaColumn, QuotaFallback
from imp.cli.render_html import _fold_markers
from imp.cli.render_summary import (
    StepRow,
    SummaryHeader,
    _dur_text,
    _fmt_tokens,
    _last_cell,
    _sub_body,
    open_span_label,
    summary_notes,
    summary_totals,
    total_cell,
)

_MAX_HEADING = 6

_ENGINE_MD_LABELS = {
    "system_prompt": "system prompt",
    "agent_prompt": "agent prompt",
    "continuation": "continuation",
    "completion": "completion nudge",
}


_Esc = Callable[[str], str]


def _escape_all(text: str) -> str:
    """Neutralize untrusted content: drop SGR color (a mockup exports as its text,
    not `^[[1;33m` noise), caret-escape the remaining C0/DEL control bytes (reusing
    render_text's control-byte guard), then HTML-escape the three significant
    characters so a `<script>` in a tool result or agent turn renders as visible
    text, never a live tag."""
    return html.escape(rt._caret_escape(rt._strip_sgr(text)), quote=False)


def _md_escape(text: str) -> str:
    return _escape_all(text)


def _commit_block(footer: str, commits: list[dict[str, str]], esc: _Esc) -> str:
    """A terminal footer line plus, when commits were recorded, an unquoted Markdown
    list of `<sha8>` + subject beneath it. One block, so the list is not separated
    from the footer by a blank line; unquoted because a bold line is not a callout."""
    return "\n".join([footer, *(f"- `{c['sha'][:8]}` {esc(c['subject'])}"
                                for c in commits)])


def _heading(level: int, text: str) -> str:
    return "#" * min(level, _MAX_HEADING) + " " + text.replace("\n", " ")


def _fence(text: str) -> str:
    """Wrap literal content in a code fence long enough to survive any backtick
    run inside it. SGR color is dropped and the remaining control bytes are
    caret-escaped; the fence itself guarantees the content is inert when later
    rendered to HTML."""
    body = rt._caret_escape(rt._strip_sgr(text))
    longest = 0
    run = 0
    for ch in body:
        run = run + 1 if ch == "`" else 0
        longest = max(longest, run)
    ticks = "`" * max(3, longest + 1)
    return f"{ticks}\n{body}\n{ticks}"


def _details(summary: str, inner: str, *, full: bool, esc: _Esc) -> str:
    """A fold: collapsed <details> in default mode, an inline bold header +
    content under --full. `summary` is raw: the bold label is Markdown, so it
    takes `esc`, while `<summary>` is raw HTML, so it always takes full escape.
    `inner` is a Markdown block already assembled from escaped content."""
    if full:
        label = f"**{esc(summary)}**"
        return f"{label}\n\n{inner}" if inner else label
    return (f"<details>\n<summary>{_escape_all(summary)}</summary>\n\n"
            f"{inner}\n\n</details>")


def _tool_call_text(c: nv.ToolCall) -> str:
    lines = [f"· {nv.display_tool_name(c.name)}"]
    for k, v in (c.input or {}).items():
        lines.append(f"  {k}={' '.join(str(v).split())}")
    if c.result:
        lines.append("")
        lines.append(c.result.rstrip("\n"))
    return "\n".join(lines)


def _subagent_status_label(sub: nv.SubagentFold) -> str:
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
    return label


def _subagent_outcome_md(sub: nv.SubagentFold, esc: _Esc) -> str:
    raw = sub.outcome or ""
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        data = None
    if not isinstance(data, dict):
        return esc(raw)
    scalars: list[str] = []
    blocks: list[tuple[str, str]] = []
    for k, v in data.items():
        if isinstance(v, str) and "\n" in v:
            blocks.append((k, v))
        else:
            scalars.append(f"{k}={v}")
    parts: list[str] = []
    if scalars:
        parts.append("→ " + esc(" · ".join(scalars)))
    for k, v in blocks:
        parts.append(f"**{esc(k)}:**\n\n{esc(v)}")
    return "\n\n".join(parts)


def _walk_children(children: list[Any], *, full: bool, cats: frozenset[str],
                   level: int, esc: _Esc) -> Iterator[str]:
    for child in children:
        yield from _walk(child, full=full, cats=cats, level=level, esc=esc)


def _walk(node: Any, *, full: bool, cats: frozenset[str], level: int,
          esc: _Esc) -> Iterator[str]:
    if isinstance(node, nv.WorkflowNode):
        yield _heading(level, esc(node.title or "imp log"))
        yield from _walk_children(node.children, full=full, cats=cats, level=level + 1, esc=esc)
        return
    if isinstance(node, nv.PhaseNode):
        yield _heading(level, esc(node.label or node.name))
        yield from _walk_children(node.children, full=full, cats=cats, level=level + 1, esc=esc)
        return
    if isinstance(node, nv.StepNode):
        yield _heading(level, esc(node.label or node.name))
        yield from _walk_children(node.children, full=full, cats=cats, level=level + 1, esc=esc)
        return
    if isinstance(node, nv.AgentTextNode):
        text = node.text.strip()
        if text:
            yield esc(rt._restore_bare_sgr_in_spans(rt._restore_bare_sgr(text)))
        return
    if isinstance(node, nv.LiteralTextNode):
        text = node.text.strip()
        if text:
            yield esc(text)
        return
    if isinstance(node, nv.EngineMessageNode):
        text = node.text.strip()
        if "prompts" not in cats or not text:
            return
        label = _ENGINE_MD_LABELS.get(node.subtype, node.subtype)
        yield _details(label, _fence(text), full=full, esc=esc)
        return
    if isinstance(node, nv.UserMessageNode):
        text = node.text.strip()
        if node.interactive and text:
            yield "\n".join("> " + line
                            for line in esc(text).split("\n"))
        return
    if isinstance(node, nv.ThinkingFold):
        content = node.content.strip()
        if content and "thinking" in cats:
            yield _details("thinking", esc(content), full=full, esc=esc)
        return
    if isinstance(node, nv.ToolBurstFold):
        calls = nv.visible_calls(node)
        if not calls:
            return
        recap = rt._burst_summary(node) or "tools"
        if "tools" not in cats:
            yield esc(recap)
            return
        inner = "\n\n".join(_fence(_tool_call_text(c)) for c in calls)
        yield _details(recap, inner, full=full, esc=esc)
        return
    if isinstance(node, nv.StepContextNode):
        if "prompts" not in cats:
            return
        p = node.payload
        provider = str(p.get("provider", "?"))
        model = str(p.get("model", "?"))
        parts = [f"provider: {provider}", f"model: {model}",
                 f"max_tool_calls: {p.get('max_tool_calls', '?')}"]
        inner_blocks = [esc(" · ".join(parts))]
        summary = f"Internal context — {provider}/{model}"
        yield _details(summary, "\n\n".join(inner_blocks), full=full, esc=esc)
        return
    if isinstance(node, nv.SubagentFold):
        label = _subagent_status_label(node)
        if "subagents" not in cats:
            yield "· " + esc(label)
            return
        body = _walk_children(node.tree.root.children, full=full, cats=cats,
                              level=level + 1, esc=esc)
        blocks = [b for b in body if b]
        outcome = _subagent_outcome_md(node, esc)
        if outcome:
            blocks.append(outcome)
        inner_text = "\n\n".join(blocks)
        if not inner_text and not full:
            yield "· " + esc(label)
            return
        summary = node.subagent_name if full else "· " + label
        yield _details(summary, inner_text, full=full, esc=esc)
        return
    if isinstance(node, nv.OutcomeLine):
        yield "→ " + esc(pal.action_text(node.action))
        return
    if isinstance(node, nv.BreadcrumbLine):
        name = node.step_label or node.step_name
        if node.interrupted:
            parts = [f"{name} interrupted"]
        elif node.show_complete:
            parts = [f"{name} · complete"]
        else:
            parts = [name]
        parts.extend(rt.round_and_tally_parts(node.round, node.counts))
        done = rt.fmt_seconds_done(node.elapsed_s)
        if done:
            parts.append(done)
        yield "_" + esc(" · ".join(parts)) + "_"
        return
    if isinstance(node, nv.SkillLoadNode):
        yield "loading skill " + esc(node.name)
        return
    if isinstance(node, nv.ErrorNode):
        if node.is_warning:
            if node.full_only and not (cats >= rt._ALL_CATEGORIES):
                return
            yield "> **warning:** " + esc(node.message)
            return
        tag = f"[{node.category}] " if node.category else ""
        block = "> **error:** " + esc(f"{tag}{node.message}")
        if node.detail and node.detail not in node.message:
            block += "\n>\n" + "\n".join(
                "> " + line for line in esc(node.detail).split("\n"))
        yield block
        return
    if isinstance(node, nv.PauseNode):
        yield f"**paused ({esc(node.reason)})**"
        return
    if isinstance(node, nv.RenameNode):
        yield 'Set name to "' + esc(node.name) + '"'
        return
    if isinstance(node, nv.WarningsSummaryNode):
        noun = "warning" if node.total == 1 else "warnings"
        lines = [f"> **{node.total} {noun} this run**"]
        for message, count in node.lines:
            suffix = f" (×{count})" if count > 1 else ""
            lines.append("> - " + esc(message) + suffix)
        yield "\n".join(lines)
        return
    if isinstance(node, nv.CompletedNode):
        yield _commit_block(f"**✓ Completed {esc(node.quest_id)}.**",
                            node.commits, esc)
        return
    if isinstance(node, nv.MergedNode):
        yield _commit_block(
            f"**✓ Merged {esc(node.quest_id)} into {esc(node.base)}.**",
            node.commits, esc)
        return
    if isinstance(node, nv.TiersResolvedNode):
        if "tiers" not in cats:
            return
        rows = "\n".join(
            f"- {esc(str(r['tier']))} → "
            f"{esc(str(r['provider']))}/{esc(str(r['model']))}"
            for r in node.rows)
        yield _details("tiers resolved", rows, full=full, esc=esc)
        return
    if isinstance(node, nv.FallbackLine):
        if not (cats >= rt._ALL_CATEGORIES):
            return
        kv = " ".join(f"{k}={v}" for k, v in node.payload.items())
        yield esc(f"{node.event_type} {kv}".rstrip())
        return
    # Additional node branches are appended by later tasks, before this return.
    return


_VIEWER = MarkdownIt("commonmark", {"html": True}).enable("table")
_ESCAPED_CONTENT = frozenset({"inline", "text", "code_inline", "fence", "code_block"})


def _token_keys(markdown: str) -> list[tuple[int, Token]]:
    """A raw-HTML-passing viewer's tokens in preorder with their depth, stripped of
    children and of the `content` its renderer escapes."""
    keys: list[tuple[int, Token]] = []
    stack = [(0, tok) for tok in reversed(_VIEWER.parse(markdown))]
    while stack:
        depth, tok = stack.pop()
        stack.extend((depth + 1, child) for child in reversed(tok.children or []))
        tok.children = None
        if tok.type in _ESCAPED_CONTENT:
            tok.content = ""
        keys.append((depth, tok))
    return keys


def _same_tokens(c: str, e: str) -> bool:
    return _token_keys(c) == _token_keys(e)


def _accept(c: str, e: str) -> bool:
    tokens_match = _same_tokens(c, e)
    markers_match = _fold_markers(c) == _fold_markers(e)
    return tokens_match and markers_match


def _guarded(render: Callable[[_Esc], str]) -> str:
    """Pass C (`_md_escape`) when it is structurally the full-escape pass E, else E."""
    c, e = render(_md_escape), render(_escape_all)
    return c if c == e or _accept(c, e) else e


def _narrative_document(tree: nv.NarrativeTree, *, full: bool, cats: frozenset[str],
                        esc: _Esc) -> str:
    blocks = [b for b in _walk(tree.root, full=full, cats=cats, level=1, esc=esc) if b]
    return "\n\n".join(blocks).strip() + "\n"


def render_markdown(tree: nv.NarrativeTree, *, full: bool,
                    show: frozenset[str] | None = None) -> str:
    """Render a NarrativeTree to a Markdown document string."""
    cats = rt._resolve_cats(full, show)
    return _guarded(lambda esc: _narrative_document(tree, full=full, cats=cats, esc=esc))


_MD_HEADER = "| Step | Duration | Fresh | Cache read | Cache write | Out | Cost |"
_MD_QUOTA_HEADER = ("| Step | Duration | Fresh | Cache read | Cache write | Out"
                    " | Weekly* |")
_MD_RULE = "|---|---|---|---|---|---|---|"


def _md_legend(text: str) -> str:
    """A leading `*` is the sigil the legend defines, not a list bullet."""
    return f"\\{text}" if text.startswith("*") else text


def _md_cells(u: RowUsage, last: str) -> str:
    return (f"{_fmt_tokens(u.fresh_input)} | {_fmt_tokens(u.cached_input)}"
            f" | {_fmt_tokens(u.cache_write)} | {_fmt_tokens(u.output)}"
            f" | {last}")


def _summary_document(summary: list[StepRow], header: SummaryHeader,
                      quota: QuotaColumn | QuotaFallback | None, esc: _Esc) -> str:
    work, total = summary_totals(summary, header.total_seconds,
                                 header.exclusions)
    open_label = open_span_label(header.status)
    column = quota if isinstance(quota, QuotaColumn) else None
    table_header = _MD_HEADER if column is None else _MD_QUOTA_HEADER
    total_last = total_cell(summary, quota)
    blocks = [
        _heading(1, esc(f"{header.workflow} — {header.quest_id}")),
        f"**Status:** {esc(header.status)}",
    ]
    cells = [total_last]
    grouped: list[tuple[str, list[str]]] = []
    for row in summary:
        if not grouped or grouped[-1][0] != row.phase:
            grouped.append((row.phase, []))
        step = esc(row.step).replace("|", "\\|")
        last = _last_cell(row.totals, row.subtree_units, column)
        cells.append(last)
        grouped[-1][1].append(
            f"| {step} | {row.dur_cell(open_label)}"
            f" | {_md_cells(row.totals, last)} |")
        for sub in row.subagents:
            body = esc(_sub_body(sub)).replace("|", "\\|")
            sub_last = _last_cell(sub.usage, sub.units, column)
            cells.append(sub_last)
            grouped[-1][1].append(
                f"| {body} | {sub.dur_cell(open_label)}"
                f" | {_md_cells(sub.usage, sub_last)} |")
    for phase, rows in grouped:
        blocks.append("\n".join([_heading(2, esc(phase)),
                                 table_header, _MD_RULE, *rows]))
    total_rows = [
        f"| total | {_dur_text(work)} | {_md_cells(total, total_last)} |"]
    # A cell's leading whitespace is dropped, so the terminal's indent is the
    # arrow the subagent rows already use.
    for p, cell in (column.breakdown if column else ()):
        name = esc(p).replace("|", "\\|")
        total_rows.append(f"| ↳ {name} |  |  |  |  |  | {cell} |")
    blocks.append("\n".join([table_header, _MD_RULE, *total_rows]))
    blocks += [_md_legend(t) for t in summary_notes(cells, quota)]
    return "\n\n".join(blocks).strip() + "\n"


def render_summary_markdown(summary: list[StepRow], *, header: SummaryHeader,
                            quota: QuotaColumn | QuotaFallback | None = None,
                            ) -> str:
    """Render the `imp log --summary` view as a Markdown document."""
    return _guarded(lambda esc: _summary_document(summary, header, quota, esc))
