# src/imp/cli/render_text.py
"""Plain-text narrative view (imp log / non-TTY). Walks a NarrativeTree and
yields wrapped ANSI lines. Finalized prose blocks are rendered through
rich.markdown.Markdown to inner width, then the magenta gutter/indent is
prefixed (§9); structural chrome (headers, breadcrumbs) stays plain.

Default: thinking and tool bursts are dropped; subagents expand (their prose +
structured return value); context folds still collapse to a one-line summary.
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
_INDENT_W = len(_INDENT)  # logical indent width per level; the gutter SUBSTITUTES
                          # within it (same 2 cols), so wrap width is invariant

_DIFFER_LABELS = {1: "vendor jump", 2: "same-vendor fallback", 3: "identical"}


def render_tree(tree: nv.NarrativeTree, *, full: bool, color: bool,
                width: int = 80) -> Iterator[str]:
    root = tree.root
    head = f"#{root.slug if root.slug is not None else '?'}  {root.title}".rstrip()
    if root.status != "running":
        head = f"{head}  ({root.status})"
    yield pal.ansi(head, *pal.NODE_COLORS["workflow"], enabled=color)
    yield from _render_children(root.children, depth=0, full=full, color=color, width=width,
                                gutters=())


def _gutter_indent(depth: int, gutters: tuple[int, ...], color: bool) -> str:
    """Build the `depth`-level indent column-by-column. Each 2-col slot is plain
    `_INDENT` unless its level is in `gutters`, where it becomes a magenta-wrapped
    `│ ` — substituting WITHIN the indent (same 2 cols), never prepended ahead of
    it, so wrap width stays invariant. The gutter is colored INDEPENDENTLY of the
    content beside it (seam 1): a magenta gutter can sit on a default-fg / red /
    dim content line. The bar is always magenta (subagent identity), regardless of
    the content's own color or outcome status."""
    cols: list[str] = []
    for level in range(depth):
        if level in gutters:
            cols.append(pal.ansi("│ ", "magenta", enabled=color))
        else:
            cols.append(_INDENT)
    return "".join(cols)


def _wrap(text: str, width: int, depth: int, color: bool, *codes: str,
          gutters: tuple[int, ...] = ()) -> list[str]:
    indent = _gutter_indent(depth, gutters, color)
    avail = max(8, width - _INDENT_W * depth)   # logical width; gutter is invariant
    out: list[str] = []
    for para in text.split("\n"):
        if not para:
            out.append(indent)                  # blank line still carries the gutter
            continue
        for line in textwrap.wrap(para, width=avail) or [""]:
            out.append(indent + pal.ansi(line, *codes, enabled=color))
    return out


def _line(text: str, depth: int, color: bool, *codes: str,
          gutters: tuple[int, ...] = ()) -> str:
    # Indent (with any gutters) is composed separately from the content so the
    # magenta gutter and the content's own color coexist on one line (seam 1).
    return _gutter_indent(depth, gutters, color) + pal.ansi(text, *codes, enabled=color)


def _render_markdown_block(text: str, width: int, depth: int, color: bool,
                           gutters: tuple[int, ...] = ()) -> list[str]:
    """Render one finalized prose block as markdown→ANSI at inner width, then
    prefix the depth gutter/indent. rich owns bold/heading/list/fence formatting;
    we own the magenta gutter. We never slice rich's output, so no escape is ever
    cut; content rich won't reflow (code fences, tables) is allowed to overflow.

    Theme override: rich's default `markdown.code` is bold+cyan+black-bg. We
    drop the bold and dark bg by setting it to plain `cyan` so inline `code`
    sits calmly in the prose. Fenced blocks (`markdown.code_block`) keep
    rich's default formatting."""
    import io

    from rich.console import Console
    from rich.markdown import Markdown
    from rich.theme import Theme

    inner = max(8, width - _INDENT_W * depth)
    buf = io.StringIO()
    console = Console(file=buf, width=inner, no_color=not color,
                      color_system="standard" if color else None,
                      force_terminal=color, soft_wrap=False, highlight=False,
                      theme=Theme({"markdown.code": "cyan"}))
    console.print(Markdown(text))
    indent = _gutter_indent(depth, gutters, color)
    out: list[str] = []
    for raw in buf.getvalue().splitlines():
        stripped = raw.rstrip()
        if not stripped:
            # Blank line inside the prose block — emit the bare gutter-indent
            # so the magenta │ stays continuous inside a SubagentFold.
            out.append(indent)
        else:
            out.append(indent + stripped)
    return out or [indent]


def is_prose_leaf(node: Any) -> bool:
    """True for the agent-speech blocks (rendered LLM prose / surfaced free-text
    output fields). Consecutive prose blocks get one blank line between them so
    separate assistant turns don't bunch when the intervening thinking/tool turns
    render nothing in lean mode. Shared by the batch walk and the live driver so
    both paths space prose identically (design §5.4)."""
    return isinstance(node, (nv.AgentTextNode, nv.LiteralTextNode))


def _render_children(children: list[Any], *, depth: int, full: bool, color: bool,
                     width: int, gutters: tuple[int, ...] = ()) -> Iterator[str]:
    """Render a container's children in order, inserting ONE blank line between
    a prose block and ANY following emission. A child that renders nothing
    (lean-mode thinking / empty tool burst) leaves the prose-adjacency state
    untouched, so prose→thinking→prose still separates; any non-empty emission
    resets it. The blank separator is the gutter-bearing indent at the child
    depth (reusing `_wrap`'s blank-paragraph carrier) so the magenta │ stays
    continuous inside a SubagentFold."""
    prev_prose = False
    for child in children:
        lines = list(_render(child, depth=depth, full=full, color=color, width=width,
                             gutters=gutters))
        if not lines:
            continue
        if prev_prose:
            yield _gutter_indent(depth, gutters, color)
        yield from lines
        prev_prose = is_prose_leaf(child)


def phase_breadcrumb_line(node: nv.PhaseNode, *, depth: int, color: bool,
                          gutters: tuple[int, ...] = ()) -> str:
    """The `✓ <phase> · <N>s` completion line, shared by both renderers (§7).
    `depth` is the phase HEADER's depth — pass `depth` from render_text and
    `cur.depth - 1` from the live driver; both equal the header level.
    This depth equivalence holds only because phases appear solely as direct
    children of the root (single-level invariant) and would break if phases
    ever nested inside SubagentFolds."""
    elapsed = nv.phase_elapsed(node)
    secs = "" if elapsed is None else f" · {int(elapsed)}s"
    return _line(f"✓ {node.name}{secs}", depth, color, *pal.NODE_COLORS["phase"],
                 gutters=gutters)


def header_line(node: Any, *, depth: int, color: bool,
                gutters: tuple[int, ...] = ()) -> str:
    """The single header line for a container node (PhaseNode / StepNode /
    SubagentFold), with no children. Shared by `_render` and the live driver so
    indentation/label/color stay identical in both paths. For a SubagentFold this
    is the `↳ task → {name}` boundary: subagent color, ↳ descent marker (not a
    fold/expand glyph), and no (running)/(error) suffix — the live driver prints
    it while the subagent is still running, so any status suffix would be
    permanently wrong once it completes (completion is signaled by the outcome
    line instead)."""
    if isinstance(node, nv.PhaseNode):
        return _line(node.name, depth, color, *pal.NODE_COLORS["phase"], gutters=gutters)
    if isinstance(node, nv.SubagentFold):
        return _line(f"↳ task → {node.subagent_name}", depth, color,
                     *pal.NODE_COLORS["subagent"], gutters=gutters)
    label = node.name
    if node.interactive:
        label += " (interactive)"
    return _line(label, depth, color, *pal.NODE_COLORS["step"], gutters=gutters)


def subagent_outcome_lines(sub: nv.SubagentFold, *, depth: int, color: bool,
                           width: int, gutters: tuple[int, ...] = ()) -> Iterator[str]:
    """The subagent's structured return value, rendered readably at the passed
    `depth` (no internal +1 — the caller passes the prose depth). Empty when the
    subagent has no outcome yet (still running / force-popped).

    On the success path the outcome is `json.dumps(...)` of the structured output
    the parent consumed, so its prose newlines are escaped to literal `\\n`; on the
    error path (a failed dispatch) it is a plain, non-JSON string. We parse it back:
    a dict renders short scalar fields inline on one `→ key=value · …` line and each
    multi-line string field as a labeled block with REAL newlines (wrapped like
    agent prose). A non-dict — including the plain-string error case — renders as
    prose. Error-styled when `sub.is_error`."""
    raw = sub.outcome or ""
    if not raw:
        return
    codes = pal.NODE_COLORS["error"] if sub.is_error else pal.NODE_COLORS["subagent"]
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        data = None
    if not isinstance(data, dict):
        yield from _wrap(raw, width, depth, color, *codes, gutters=gutters)
        return
    scalars: list[str] = []
    blocks: list[tuple[str, str]] = []
    for k, v in data.items():
        if isinstance(v, str) and "\n" in v:
            blocks.append((k, v))
        else:
            scalars.append(f"{k}={v}")
    if scalars:
        yield _line("→ " + " · ".join(scalars), depth, color, *codes, gutters=gutters)
    for k, v in blocks:
        yield _line(f"{k}:", depth, color, *codes, gutters=gutters)
        yield from _wrap(v, width, depth + 1, color, *codes, gutters=gutters)


def _render(node: Any, *, depth: int, full: bool, color: bool,
            width: int, gutters: tuple[int, ...] = ()) -> Iterator[str]:
    if isinstance(node, nv.PhaseNode):
        yield header_line(node, depth=depth, color=color, gutters=gutters)
        yield from _render_children(node.children, depth=depth + 1, full=full,
                                    color=color, width=width, gutters=gutters)
        if node.closed and nv.phase_elapsed(node) is not None:
            yield phase_breadcrumb_line(node, depth=depth, color=color, gutters=gutters)
        return

    if isinstance(node, nv.StepNode):
        yield header_line(node, depth=depth, color=color, gutters=gutters)
        yield from _render_children(node.children, depth=depth + 1, full=full,
                                    color=color, width=width, gutters=gutters)
        return

    if isinstance(node, nv.AgentTextNode):
        yield from _render_markdown_block(node.text, width, depth, color, gutters)
        return

    if isinstance(node, nv.LiteralTextNode):
        yield from _render_markdown_block(node.text, width, depth, color, gutters)
        return

    if isinstance(node, nv.UserMessageNode):
        yield from _wrap(f"> {node.text}", width, depth, color,
                         *pal.NODE_COLORS["user"], gutters=gutters)
        return

    if isinstance(node, nv.ThinkingFold):
        if not node.content.strip():
            return  # blank/whitespace thinking is pure noise — drop in both modes
        if full:
            yield _line(f"{pal.fold_glyph(expanded=True)} thinking", depth, color,
                        *pal.NODE_COLORS["thinking"], gutters=gutters)
            yield from _wrap(node.content, width, depth + 1, color,
                             *pal.NODE_COLORS["thinking"], gutters=gutters)
        # full=False: thinking is noise in the default view — render nothing.
        # (A transient `· thinking` status cue lives in narrative.current_activity.)
        return

    if isinstance(node, nv.ToolBurstFold):
        yield from _render_burst(node, depth=depth, full=full, color=color, width=width,
                                 gutters=gutters)
        return

    if isinstance(node, nv.SubagentFold):
        yield from _render_subagent(node, depth=depth, full=full, color=color, width=width,
                                    gutters=gutters)
        return

    if isinstance(node, nv.StepContextNode):
        yield from _render_step_context(node, depth=depth, full=full, color=color, width=width,
                                        gutters=gutters)
        return

    if isinstance(node, nv.GateNode):
        yield from _render_gate(node, depth=depth, color=color, width=width, gutters=gutters)
        return

    if isinstance(node, nv.OutcomeLine):
        text = pal.action_text(node.action)
        ac = pal.accent_color(node.action)
        codes = (ac,) if ac else ()
        yield _line(f"→ {text}", depth, color, *codes, gutters=gutters)
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
        yield _line(" · ".join(parts), depth, color, *codes, gutters=gutters)
        return

    if isinstance(node, nv.ErrorNode):
        if node.is_warning:
            yield _line(f"warning: {node.message}", depth, color,
                        *pal.NODE_COLORS["warning"], gutters=gutters)
        else:
            tag = f"[{node.category}] " if node.category else ""
            yield _line(f"error: {tag}{node.message}", depth, color,
                        *pal.NODE_COLORS["error"], gutters=gutters)
        return

    if isinstance(node, nv.RolesResolvedNode):
        yield _line("roles resolved:", depth, color, gutters=gutters)
        col_w = max((len(r["role"]) for r in node.rows), default=0)  # role-name column
        for r in node.rows:
            # two-space sub-indent under the "roles resolved:" header
            line = f"  {r['role']:<{col_w}} → {r['provider']}/{r['model']}"
            if r["differ_tier"] is not None:
                label = _DIFFER_LABELS.get(r["differ_tier"], "?")
                line += (f"   (differ from {r['differ_from']}: "
                         f"tier {r['differ_tier']}, {label})")
            yield _line(line, depth, color, gutters=gutters)
        return

    if isinstance(node, nv.FallbackLine):
        kv = " ".join(f"{k}={v}" for k, v in node.payload.items())
        yield _line(f"{node.event_type}  {kv}".rstrip(), depth, color, "dim",
                    gutters=gutters)
        return


def _render_burst(burst: nv.ToolBurstFold, *, depth: int, full: bool,
                  color: bool, width: int,
                  gutters: tuple[int, ...] = ()) -> Iterator[str]:
    if not full:
        return  # tool bursts are noise in the default view — render nothing.
    yield _line(f"{pal.fold_glyph(expanded=True)} tools", depth, color,
                *pal.NODE_COLORS["tools"], gutters=gutters)
    for c in burst.calls:
        args = " ".join(f"{k}={v}" for k, v in (c.input or {}).items())
        killed = " [killed]" if c.killed else ""
        result = (c.result or "").replace("\n", " ")
        if len(result) > 60:
            result = result[:60] + "…"
        line = f"⏺ {c.name}  {args}  → {result}{killed}".rstrip()
        yield from _wrap(line, width, depth + 1, color, *pal.NODE_COLORS["tools"],
                         gutters=gutters)


def _render_subagent(sub: nv.SubagentFold, *, depth: int, full: bool,
                     color: bool, width: int,
                     gutters: tuple[int, ...] = ()) -> Iterator[str]:
    # Header: lean uses the shared `↳ task → {name}` boundary (↳ descent glyph, shared
    # with the live driver); --full uses the expanded `▾ task → {name}` fold header.
    if full:
        yield _line(f"{pal.fold_glyph(expanded=True)} task → {sub.subagent_name}",
                    depth, color, *pal.NODE_COLORS["subagent"], gutters=gutters)
    else:
        yield header_line(sub, depth=depth, color=color, gutters=gutters)
    # The subagent's own narrative, recursed at the same `full` level (lean filters
    # nested thinking/tools via A1/A2; --full expands them).
    yield from _render_children(sub.tree.root.children, depth=depth + 1, full=full,
                                color=color, width=width, gutters=(*gutters, depth))
    # The structured return value the parent consumed — emitted in BOTH modes so
    # --full stays a superset of default (DEVIATION from spec, which left --full
    # untouched: the spec's "--full unchanged" produced the backwards result of
    # --full showing LESS than default; per maintainer decision, --full is now a
    # superset again). Always emit when non-empty (decision ii).
    yield from subagent_outcome_lines(sub, depth=depth + 1, color=color, width=width,
                                      gutters=(*gutters, depth))


def _render_step_context(ctx: nv.StepContextNode, *, depth: int, full: bool,
                         color: bool, width: int,
                         gutters: tuple[int, ...] = ()) -> Iterator[str]:
    p = ctx.payload
    provider = p.get("provider", "?")
    model = p.get("model", "?")
    tools = p.get("tools_schema") or []
    if not full:
        yield _line(f"{pal.fold_glyph(expanded=False)} context · {provider}/{model} · "
                    f"{len(tools)} tools", depth, color, *pal.NODE_COLORS["metadata"],
                    gutters=gutters)
        return
    yield _line(f"{pal.fold_glyph(expanded=True)} context · {provider}/{model} · "
                f"max_tool_calls={p.get('max_tool_calls', '?')}", depth, color,
                *pal.NODE_COLORS["metadata"], gutters=gutters)
    yield _line("system_prompt:", depth + 1, color, *pal.NODE_COLORS["metadata"],
                gutters=gutters)
    yield from _wrap(str(p.get("system_prompt", "")), width, depth + 2, color,
                     *pal.NODE_COLORS["metadata"], gutters=gutters)
    yield _line("tools:", depth + 1, color, *pal.NODE_COLORS["metadata"], gutters=gutters)
    for t in tools:
        name = t.get("name", t) if isinstance(t, dict) else t
        yield _line(f"- {name}", depth + 2, color, *pal.NODE_COLORS["metadata"],
                    gutters=gutters)


def _render_gate(gate: nv.GateNode, *, depth: int, color: bool, width: int,
                 recap: bool = True, gutters: tuple[int, ...] = ()) -> Iterator[str]:
    """Render a gate node. `recap=True` (imp log) emits header + prompt + every
    option — the permanent record. `recap=False` (live driver) emits the header
    line only, because run_human_gate already showed the prompt/options as live
    chrome; re-rendering them would duplicate a multi-line block in scroll-back."""
    action = pal.action_text(gate.action) if gate.action is not None else "pending"
    accent = pal.accent_color(gate.action) if gate.action is not None else None
    codes = (accent,) if accent else pal.NODE_COLORS["gate"]
    header = f"{gate.kind} gate → {action}"
    yield _line(header, depth, color, *codes, gutters=gutters)
    if recap and gate.prompt:
        yield from _wrap(gate.prompt, width, depth + 1, color, *pal.NODE_COLORS["gate"],
                         gutters=gutters)
        for opt in gate.options:
            yield _line(f"- {opt.get('label', opt)}", depth + 1, color,
                        *pal.NODE_COLORS["gate"], gutters=gutters)


