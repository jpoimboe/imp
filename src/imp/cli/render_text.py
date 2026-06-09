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


def fmt_seconds(seconds: float | int | None) -> str:
    """Format an elapsed-second count for a completion/recap line. Under a minute
    reads as `Ns`; a minute or more as `Mm Ss` (e.g. 336 → `5m 36s`) so a long
    step reads in human time instead of a wall of seconds. None → empty string."""
    if seconds is None:
        return ""
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    return f"{total // 60}m {total % 60}s"


def render_tree(tree: nv.NarrativeTree, *, full: bool, color: bool,
                width: int = 80) -> Iterator[str]:
    # No slug/title header: the transcript opens directly on the first turn. The
    # change id/title belong to `imp` chrome (the run header, `imp list`), not the
    # narrative body — so live and log both start with content, not a banner.
    root = tree.root
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


def _hang_wrap(text: str, marker: str, width: int, depth: int, color: bool,
               *codes: str, gutters: tuple[int, ...] = ()) -> list[str]:
    """Wrap `text` with a `marker` ("> ", "· ") on the first line and a matching-
    width blank hang on every continuation line, so wrapped text lines up under
    the words after the marker (not under the marker itself)."""
    indent = _gutter_indent(depth, gutters, color)
    hang = " " * len(marker)
    avail = max(8, width - _INDENT_W * depth - len(marker))
    out: list[str] = []
    first = True
    for para in text.split("\n"):
        if not para:
            out.append(indent)
            continue
        for line in textwrap.wrap(para, width=avail) or [""]:
            out.append(indent + pal.ansi((marker if first else hang) + line,
                                         *codes, enabled=color))
            first = False
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

    # An agent turn is marked with a leading bold "• " bullet and hang-indented:
    # the bullet sits at the base column and every wrapped/continuation line aligns
    # 2 cols in, under the first word. (A bold dot — heavier than the small
    # "·" chrome separators — so the turn marker clearly leads.) Render markdown 2
    # cols narrower to leave room for the hang, then place the bullet on the first
    # non-blank line and the hang on the rest.
    inner = max(8, width - _INDENT_W * depth - 2)
    buf = io.StringIO()
    console = Console(file=buf, width=inner, no_color=not color,
                      color_system="standard" if color else None,
                      force_terminal=color, soft_wrap=False, highlight=False,
                      theme=Theme({"markdown.code": "cyan"}))
    console.print(Markdown(text))
    indent = _gutter_indent(depth, gutters, color)
    bullet = pal.ansi("• ", "bold", enabled=color)   # bold dot marks the turn
    out: list[str] = []
    first = True
    for raw in buf.getvalue().splitlines():
        stripped = raw.rstrip()
        if not stripped:
            # Blank line inside the prose block — emit the bare gutter-indent
            # so the magenta │ stays continuous inside a SubagentFold.
            out.append(indent)
        elif first:
            out.append(indent + bullet + stripped)
            first = False
        else:
            out.append(indent + "  " + stripped)
    return out or [indent + bullet]


def is_prose_leaf(node: Any) -> bool:
    """True for the agent-speech blocks (rendered LLM prose / surfaced free-text
    output fields). Consecutive prose blocks get one blank line between them so
    separate assistant turns don't bunch when the intervening thinking/tool turns
    render nothing in lean mode. Shared by the batch walk and the live driver so
    both paths space prose identically (design §5.4)."""
    return isinstance(node, (nv.AgentTextNode, nv.LiteralTextNode))


def spaces_after(node: Any, *, full: bool) -> bool:
    """A "spaced block": one a blank separator follows (and precedes, when it
    trails another spaced block). Prose leaves and user turns always qualify; a
    collapsed tool-burst summary qualifies in the lean view, where it sits as a
    single line between two prose turns and wants air on both sides (so the run
    reads `…prose / · burst / prose…` rather than bunching). Structural chrome —
    a phase header/complete line, a step (which ends on its completion
    breadcrumb), and errors — is surrounded by blank lines so each stands clear of
    the prose around it. The batch walk and the live driver both key spacing off
    this, so they stay identical (StepNode/PhaseNode matter only to the batch
    walk, which treats a container as one child; live keys off the last leaf —
    the breadcrumb — instead, and they agree)."""
    return bool(
        is_prose_leaf(node)
        or isinstance(node, (nv.UserMessageNode, nv.SkillLoadNode, nv.SubagentFold,
                             nv.PhaseNode, nv.StepNode, nv.BreadcrumbLine,
                             nv.ErrorNode, nv.PauseNode))
        or (isinstance(node, nv.ToolBurstFold) and not full and node.calls)
    )


def spaces_before(node: Any) -> bool:
    """A node that wants a blank line ABOVE it regardless of what preceded it.
    An interactive step (the human's turn is a conversational beat), a phase
    header (a new phase starts on its own), an error (it must stand clear of the
    line before it), and a chrome step (a bare completion breadcrumb — it's
    surrounded like a leaf, since nothing else inside it carries spacing) all
    qualify. A regular step-completion breadcrumb is NOT here: it already trails a
    spaced block (the step's final prose / the header gap), so it gets its
    blank-above for free without risking a doubled separator. The batch walk and
    the live driver both key off this so the lead-in blank lands identically in
    `imp log` and the live view."""
    return bool(
        (isinstance(node, nv.StepNode) and node.interactive)
        or is_chrome_step(node)
        or isinstance(node, (nv.PhaseNode, nv.ErrorNode, nv.PauseNode))
    )


_CHROME_KINDS = ("script", "branch", "merge")


def is_chrome_step(node: Any) -> bool:
    """True for a no-op structural step (a script gate / branch) whose whole
    contribution is a completion breadcrumb. These collapse to that single
    dark-gray line — the redundant `name` header above the `name complete · 0s`
    line is pure noise. Agent (and human) steps carry real content and keep their
    header."""
    return isinstance(node, nv.StepNode) and node.kind in _CHROME_KINDS


def _render_children(children: list[Any], *, depth: int, full: bool, color: bool,
                     width: int, gutters: tuple[int, ...] = (),
                     prime: bool = False) -> Iterator[str]:
    """Render a container's children in order, inserting ONE blank line between
    a prose block and ANY following emission. A child that renders nothing
    (lean-mode thinking / empty tool burst) leaves the prose-adjacency state
    untouched, so prose→thinking→prose still separates; any non-empty emission
    resets it. The blank separator is the gutter-bearing indent at the child
    depth (reusing `_wrap`'s blank-paragraph carrier) so the magenta │ stays
    continuous inside a SubagentFold. `prime=True` seeds the prose-adjacency
    state so the FIRST child gets a blank above it — used to put air below a
    phase's start line before its first step."""
    prev_prose = prime
    for child in children:
        lines = list(_render(child, depth=depth, full=full, color=color, width=width,
                             gutters=gutters))
        if not lines:
            continue
        if prev_prose or spaces_before(child):
            yield _gutter_indent(depth, gutters, color)
        yield from lines
        prev_prose = spaces_after(child, full=full)


def phase_breadcrumb_line(node: nv.PhaseNode, *, depth: int, color: bool,
                          gutters: tuple[int, ...] = ()) -> str:
    """The `✓ <phase> · <N>s` completion line, shared by both renderers (§7).
    `depth` is the phase HEADER's depth — pass `depth` from render_text and
    `cur.depth - 1` from the live driver; both equal the header level.
    This depth equivalence holds only because phases appear solely as direct
    children of the root (single-level invariant) and would break if phases
    ever nested inside SubagentFolds."""
    elapsed = nv.phase_elapsed(node)
    secs = "" if elapsed is None else f" · {fmt_seconds(elapsed)}"
    # No checkmark, and dark gray (the breadcrumb color) — identical treatment to
    # the step-completion line, so the whole family of `<name> complete · …` lines
    # reads consistently as quiet chrome rather than the bold blue of a phase header.
    return _line(f"{node.name} complete{secs}", depth, color,
                 *pal.NODE_COLORS["breadcrumb"], gutters=gutters)


def header_line(node: Any, *, depth: int, color: bool,
                gutters: tuple[int, ...] = ()) -> str:
    """The single header line for a container node (PhaseNode / StepNode /
    SubagentFold), with no children. Shared by `_render` and the live driver so
    indentation/label/color stay identical in both paths. For a SubagentFold this
    is the `· {name}` boundary: subagent color, a small dot (no "subagent" word,
    no arrow), and no status suffix. Note: the lean/live views no longer use this
    for subagents — they emit `subagent_status_row`. This bare header survives only
    for the --full expanded fold and PhaseNode/StepNode."""
    if isinstance(node, nv.PhaseNode):
        return _line(node.name, depth, color, *pal.NODE_COLORS["phase"], gutters=gutters)
    if isinstance(node, nv.SubagentFold):
        return _line(f"· {node.subagent_name}", depth, color,
                     *pal.NODE_COLORS["subagent"], gutters=gutters)
    label = node.name
    if node.model:
        # The model rides on the step line itself (design: drop the separate
        # `▸ context · provider/model · N tools` line from the lean view — it
        # survives only in `imp log --full`). Same gray as the rest of the step
        # chrome, one space around the `·` to match every other dotted line; the
        # provider and tool count are detail that the full view owns. An
        # interactive step carries no `(interactive)` tag — its blank-line lead-in
        # (spaces_before) is the only marker it needs.
        label += f" · {node.model}"
    return _line(label, depth, color, *pal.NODE_COLORS["step"], gutters=gutters)


def _subagent_model(sub: nv.SubagentFold) -> str | None:
    """The subagent's model, dug out of its nested narrative (its step_context /
    step carries provider+model). Returns the first model found, or None."""
    def find(nodes: list[Any]) -> str | None:
        for n in nodes:
            if isinstance(n, nv.StepNode):
                if n.model:
                    return n.model
                inner = find(n.children)
                if inner:
                    return inner
            elif isinstance(n, (nv.StepContextNode, nv.AgentTextNode)):
                m = n.payload.get("model")
                if m:
                    return str(m)
        return None
    return find(sub.tree.root.children)


def subagent_status_row(sub: nv.SubagentFold, *, depth: int, color: bool,
                        gutters: tuple[int, ...] = ()) -> str:
    """The single lean/live row for a subagent: `{name}` — and NOTHING else (no
    prompt, no streamed output, no outcome prose). This is what structurally
    eliminates the SDK cancellation-sentinel leak from the default views. No
    leading bullet: the row is indented two cols (like the tool-burst recap) to
    sit under the agent turn's words, and the magenta color marks it as subagent
    identity. A finished subagent appends `· {elapsed}` (how long it ran); an
    abnormal terminal state (running-at-pause, failed, stalled) appends a plain
    `(state)` tag so a subagent that didn't cleanly finish still stands out."""
    label = f"  {sub.subagent_name}"
    model = _subagent_model(sub)
    if model:
        # Show the subagent's model on the row, the same way a step header does.
        label += f" · {model}"
    if sub.running:
        label += " (running)"
    elif sub.terminal_state == "stalled":
        label += " (stalled)"
    elif sub.terminal_state == "interrupted":
        label += " (interrupted)"
    elif sub.is_error:
        label += " (failed)"
    elapsed = nv._elapsed_seconds(sub.start_ts, sub.end_ts)
    if not sub.running and elapsed is not None:
        label += f" · {fmt_seconds(elapsed)}"
    return _line(label, depth, color, *pal.NODE_COLORS["subagent"], gutters=gutters)


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
        # The phase start line is surrounded by blank lines: the blank ABOVE comes
        # from the root walk (spaces_before(PhaseNode)); the blank BELOW comes from
        # priming the children walk so the first step gets a lead-in blank.
        yield header_line(node, depth=depth, color=color, gutters=gutters)
        yield from _render_children(node.children, depth=depth + 1, full=full,
                                    color=color, width=width, gutters=gutters,
                                    prime=True)
        if node.closed and nv.phase_elapsed(node) is not None:
            # Blank ABOVE the phase-complete line (it's surrounded too); the blank
            # BELOW comes from the root walk (spaces_after(PhaseNode)).
            yield _gutter_indent(depth, gutters, color)
            yield phase_breadcrumb_line(node, depth=depth, color=color, gutters=gutters)
        return

    if isinstance(node, nv.StepNode):
        # A step's content flows at the step's OWN indent (no +1): the header is a
        # label and its prose/breadcrumb align directly beneath it, rather than
        # indenting a further level and burying the agent's words. Phases still
        # indent their steps (the +1 lives in PhaseNode above); subagent folds add
        # their own level + gutter internally. (design: aligned indent)
        if is_chrome_step(node):
            # A no-op gate/script step collapses to its single dark-gray
            # breadcrumb (`✓ name · 0s`); the name-only header above it is noise.
            yield from _render_children(node.children, depth=depth, full=full,
                                        color=color, width=width, gutters=gutters)
            return
        yield header_line(node, depth=depth, color=color, gutters=gutters)
        # One blank line between the step header and the agent's first words, so the
        # speaker label and the speech don't bunch (design: §5.4 air). Priming the
        # children walk (rather than emitting the blank here) means a first child
        # that ALSO wants a blank above — an error — gets a single separator, not a
        # doubled one.
        yield from _render_children(node.children, depth=depth, full=full,
                                    color=color, width=width, gutters=gutters,
                                    prime=True)
        return

    if isinstance(node, nv.AgentTextNode):
        yield from _render_markdown_block(node.text, width, depth, color, gutters)
        return

    if isinstance(node, nv.LiteralTextNode):
        yield from _render_markdown_block(node.text, width, depth, color, gutters)
        return

    if isinstance(node, nv.UserMessageNode):
        if not node.interactive:
            # Synthetic engine prompt (the "begin" kickoff, a resume nudge) — not
            # something the human typed, so it never renders as a `>` turn.
            return
        # The user's turn renders at the node's own depth (the step's content
        # column), so the `> ` caret lines up with the step header / agent bullet
        # rather than hanging out at the far-left margin. A bright-blue caret leads
        # the first line; the typed text stays in the terminal's default fg.
        # Continuation lines hang-indent under the text (a blank the width of the
        # caret) so the message body aligns with ITSELF. Each line carries the
        # subtle "entered input" background (the replacement for the old gray
        # rule), opened AFTER the structural indent so the highlight hugs the
        # content (not the indent): the caret returns to default fg (39), NOT a
        # full reset, so the bg keeps painting under the text; one reset closes it.
        indent = _gutter_indent(depth, gutters, color)
        # M3: the pending-input question (from the awaiting_user_input event)
        # renders as a quiet line ABOVE the reply caret, so the recorded
        # transcript shows what the human was asked before what they typed.
        if node.pending_question:
            yield from _wrap(node.pending_question, width, depth, color,
                             *pal.NODE_COLORS["metadata"], gutters=gutters)
        bg = pal.user_bg_open(enabled=color)
        if color:
            caret = pal.sgr(*pal.NODE_COLORS["user"], enabled=True) + pal.CARET + "\x1b[39m"
        else:
            caret = pal.CARET
        hang = " " * len(pal.CARET)
        avail = max(8, width - _INDENT_W * depth - len(pal.CARET))
        first = True
        for para in node.text.split("\n"):
            if not para:
                yield indent
                continue
            for line in textwrap.wrap(para, width=avail) or [""]:
                prefix = bg + (caret if first else hang)
                # Close the line only when it opened an escape (a bg and/or the
                # colored caret); a plain hang under the ansi theme / color-off
                # stays bare — no dangling reset.
                yield indent + prefix + line + ("\x1b[0m" if "\x1b" in prefix else "")
                first = False
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

    if isinstance(node, nv.OutcomeLine):
        text = pal.action_text(node.action)
        ac = pal.accent_color(node.action)
        codes = (ac,) if ac else ()
        yield _line(f"→ {text}", depth, color, *codes, gutters=gutters)
        return

    if isinstance(node, nv.BreadcrumbLine):
        # No checkmark: a completed step reads as `<name> complete · …` — the word
        # carries the meaning, and dropping the glyph keeps the gray line quiet
        # (the `✓` drew the eye to navigational chrome it shouldn't). The output
        # args (status=…, *_path=…, plan counts) are detail the lean view doesn't
        # need beside every step — they survive in --full only; the routing verb
        # and elapsed time stay in both (they're outcome, not args).
        parts = [f"{node.step_name} complete" if node.show_complete else node.step_name]
        if full:
            parts.extend(node.extras)
        verb_text = pal.action_text(node.action) if node.action is not None else None
        if verb_text and verb_text != "continue":
            parts.append(verb_text)
        if node.elapsed_s is not None:
            parts.append(fmt_seconds(node.elapsed_s))
        # A step's completion line carries the step color (the same bold darker-blue
        # as its header), so a step's open and close read as one unit — distinct from
        # the dim gray of tool/skill chrome.
        yield _line(" · ".join(parts), depth, color, *pal.NODE_COLORS["step"],
                    gutters=gutters)
        return

    if isinstance(node, nv.PauseNode):
        # A resumable stop — dim `⏸ paused (reason)`, the same quiet treatment the
        # live view gives every pause (_emit_paused / _emit_paused_summary), so a
        # pause never reads as an `error:` in `imp log`. The driving failure, if
        # any, was already shown as its own `error:` event above.
        yield _line(f"⏸ paused ({node.reason})", depth, color,
                    *pal.NODE_COLORS["metadata"], gutters=gutters)
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

    if isinstance(node, nv.SkillLoadNode):
        # A loaded skill, shown once as a quiet `load skill {name}` line (the
        # redundant `load_skill` tool call is dropped from the burst recap). No
        # checkmark — indented two cols like the other sub-detail chrome.
        yield _line(f"  load skill {node.name}", depth, color,
                    *pal.NODE_COLORS["breadcrumb"], gutters=gutters)
        return

    if isinstance(node, nv.TiersResolvedNode):
        if not full:
            # Run-level tier resolution is setup noise at the top of every run —
            # dropped from the lean view; it survives in imp log --full below.
            return
        # Setup metadata: dim the whole block so it recedes like the context fold
        # rather than shouting at the top of every run.
        dim = pal.NODE_COLORS["metadata"]
        yield _line("tiers resolved:", depth, color, *dim, gutters=gutters)
        col_w = max((len(r["tier"]) for r in node.rows), default=0)  # tier-name column
        for r in node.rows:
            # two-space sub-indent under the "tiers resolved:" header
            yield _line(f"  {r['tier']:<{col_w}} → {r['provider']}/{r['model']}",
                        depth, color, *dim, gutters=gutters)
            if r["differ_tier"] is not None:
                # On its OWN line (not appended) so a long note can't overflow the
                # row and hard-wrap mid-word; indented to sit under the row.
                label = _DIFFER_LABELS.get(r["differ_tier"], "?")
                yield _line(f"    differ from {r['differ_from']}: "
                            f"tier {r['differ_tier']}, {label}",
                            depth, color, *dim, gutters=gutters)
            if r.get("warning"):
                yield _line(f"    ⚠ {r['warning']}", depth, color, *dim, gutters=gutters)
        return

    if isinstance(node, nv.FallbackLine):
        kv = " ".join(f"{k}={v}" for k, v in node.payload.items())
        yield _line(f"{node.event_type}  {kv}".rstrip(), depth, color,
                    *pal.NODE_COLORS["breadcrumb"], gutters=gutters)
        return


def _burst_summary(burst: nv.ToolBurstFold) -> str | None:
    """One-line recap of a finished tool burst for the lean view: the distinct
    tool names (first-seen order, capped), the call count, and the burst duration.
    Mirrors what the live status line showed WHILE the burst ran, left behind as a
    quiet marker so a `prose → tools → prose` turn shows the work happened without
    expanding it. Two names are omitted: `load_skill` (it renders as its own
    `load skill …` line, so counting it here would double up) and the bare
    `tool` placeholder (the sentinel name for an orphan/unnamed result — no real
    tool info). Returns None when nothing renderable remains."""
    _OMIT = {"load_skill", "tool"}
    calls = [c for c in burst.calls if c.name not in _OMIT]
    if not calls:
        return None
    names = list(dict.fromkeys(c.name for c in calls))
    shown = " · ".join(names[:4])
    more = f" +{len(names) - 4}" if len(names) > 4 else ""
    n = len(calls)
    elapsed = nv._elapsed_seconds(burst.start_ts, burst.end_ts)
    secs = "" if elapsed is None else f" · {fmt_seconds(elapsed)}"
    # No leading marker: a tool burst is sub-detail of the agent's turn, so it
    # carries no dot — it's indented (by _render_burst) to sit under the turn's
    # words instead. Internal `·` separators stay.
    return f"{shown}{more} · {n} call{'s' if n != 1 else ''}{secs}"


def _render_burst(burst: nv.ToolBurstFold, *, depth: int, full: bool,
                  color: bool, width: int,
                  gutters: tuple[int, ...] = ()) -> Iterator[str]:
    if not full:
        # Lean: leave a single dark-gray recap line (not the full per-call dump),
        # so interleaved tool work stays visible between prose turns (point 8).
        summary = _burst_summary(burst)
        if summary is not None:
            # Indent 2 cols so the burst lines up under the agent turn's words
            # (the column after the turn's "· "), reading as sub-detail of it.
            yield _line("  " + summary, depth, color,
                        *pal.NODE_COLORS["tools"], gutters=gutters)
        return
    yield _line(f"{pal.fold_glyph(expanded=True)} tools", depth, color,
                *pal.NODE_COLORS["tools"], gutters=gutters)
    for c in burst.calls:
        args = " ".join(f"{k}={v}" for k, v in (c.input or {}).items())
        killed = " [killed]" if c.killed else ""
        result = (c.result or "").replace("\n", " ")
        if len(result) > 60:
            result = result[:60] + "…"
        line = f"· {c.name}  {args}  → {result}{killed}".rstrip()
        yield from _wrap(line, width, depth + 1, color, *pal.NODE_COLORS["tools"],
                         gutters=gutters)


def _render_subagent(sub: nv.SubagentFold, *, depth: int, full: bool,
                     color: bool, width: int,
                     gutters: tuple[int, ...] = ()) -> Iterator[str]:
    if not full:
        # Lean: ONE status row, no children, no outcome (Part D). Structurally
        # eliminates the SDK cancellation-sentinel leak — lean renders no subagent
        # I/O at all, only `· {name}` (plus elapsed / abnormal-state tag).
        yield subagent_status_row(sub, depth=depth, color=color, gutters=gutters)
        return
    # --full: expanded `▾ {name}` fold header, full recursion of the subagent's
    # narrative (nested thinking/tools expanded), then the structured return value.
    # --full stays a superset of the lean view.
    yield _line(f"{pal.fold_glyph(expanded=True)} {sub.subagent_name}",
                depth, color, *pal.NODE_COLORS["subagent"], gutters=gutters)
    yield from _render_children(sub.tree.root.children, depth=depth + 1, full=full,
                                color=color, width=width, gutters=(*gutters, depth))
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
        # Lean view: the model now rides on the step header (see header_line);
        # the provider and tool count are detail the eye doesn't need every step,
        # so render nothing here. The full block below survives for imp log --full.
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
