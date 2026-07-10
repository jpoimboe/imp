# src/imp/cli/render_text.py
"""Plain-text narrative view (imp log / non-TTY). Walks a NarrativeTree and
yields wrapped ANSI lines. Finalized prose blocks are rendered through
rich.markdown.Markdown to inner width, then the magenta gutter/indent is
prefixed (§9); structural chrome (headers, breadcrumbs) stays plain.

Default: thinking and tool bursts are dropped; subagents expand (their prose +
structured return value); context folds still collapse to a one-line summary.
--full: every fold expanded inline (Task 13).
"""
from __future__ import annotations

import functools
import json
import textwrap
from collections.abc import Iterable, Iterator
from typing import Any

from imp.cli import narrative as nv
from imp.cli import palette as pal

_INDENT = "  "          # two spaces per tree level
_INDENT_W = len(_INDENT)  # logical indent width per level; the gutter SUBSTITUTES
                          # within it (same 2 cols), so wrap width is invariant

_DIFFER_LABELS = {1: "vendor jump", 2: "same-vendor fallback", 3: "identical"}

# Plain-speak copy for pause reasons that need more than the verbatim token.
# imp-canon nouns (Change, resume); unmapped reasons render as paused (<reason>).
_PAUSE_REASON_COPY: dict[str, str] = {
    "reauth_needed":
        "authentication kept failing; the Change is paused and resumable; "
        "re-authenticate to Claude (e.g. re-login), then resume",
}


def _caret_escape(text: str) -> str:
    """Map C0 control bytes (U+0000-U+001F) and DEL (U+007F) to caret notation
    (the cat -v / less convention: c -> "^" + chr(ord(c) ^ 0x40)), so a stray
    BEL/ESC/NUL/CR in content cannot beep or alter terminal state. \\t and \\n are
    load-bearing for the renderer's wrapping/layout and pass through unescaped; \\r
    IS escaped (a bare CR rewrites the current TTY line). Pure total function over
    str — run on content-origin strings BEFORE imp wraps its own SGR codes around
    them, never on the composed colored output."""
    out: list[str] = []
    for ch in text:
        if ch in ("\t", "\n"):
            out.append(ch)
            continue
        o = ord(ch)
        if o <= 0x1F or o == 0x7F:
            out.append("^" + chr(o ^ 0x40))
        else:
            out.append(ch)
    return "".join(out)


def fmt_seconds(seconds: float | int | None) -> str:
    """Format an elapsed-second count. This is the single duration formatter for
    every surface (tree recaps, live timer, summary tables, markdown). Under a
    minute reads as `Ns`; a minute or more as `Mm Ss` (e.g. 336 → `5m 36s`); an
    hour or more as `Hh Mm` (e.g. 4536 → `1h 15m` — seconds are noise at that
    scale). None → empty string."""
    if seconds is None:
        return ""
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m {total % 60}s"
    return f"{total // 3600}h {total % 3600 // 60}m"


def fmt_seconds_done(seconds: float | int | None) -> str:
    if seconds is None or seconds < 1:
        return ""
    return fmt_seconds(seconds)


def render_tree(tree: nv.NarrativeTree, *, full: bool, color: bool,
                width: int = 80, display_width: int = 80,
                live_viewer: bool = False) -> Iterator[str]:
    # No slug/title header: the transcript opens directly on the first turn. The
    # change id/title belong to `imp` chrome (the run header, `imp show`), not the
    # narrative body — so live and log both start with content, not a banner.
    root = tree.root
    yield from _render_children(root.children, depth=0, full=full, color=color, width=width,
                                display_width=display_width, gutters=(), live_viewer=live_viewer)


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


@functools.cache
def _no_bg_code_block_cls() -> type:
    """A `CodeBlock` element that renders fenced code with NO background fill.

    rich's default paints the monokai background across the full block width, so
    the bar bleeds to the right edge while the left is indented — asymmetric. We
    render the same syntax-highlighted tokens on the terminal's own background
    (`background_color="default"`, `padding=0`), the same calm treatment inline
    `code` already gets, so a block just reads as indented colored code."""
    from rich.markdown import CodeBlock
    from rich.syntax import Syntax

    class _NoBgCodeBlock(CodeBlock):
        def __rich_console__(self, console, options):  # type: ignore[no-untyped-def]
            code = str(self.text).rstrip()
            yield from console.render(
                Syntax(code, self.lexer_name, theme=self.theme, word_wrap=True,
                       padding=0, background_color="default"), options)

    return _NoBgCodeBlock


@functools.cache
def _capped_hr_cls(display_width: int) -> type:
    """Gray `─` HorizontalRule capped at the display width, matching `palette.rule()`."""
    from rich.markdown import HorizontalRule
    from rich.text import Text

    class _CappedHR(HorizontalRule):
        def __rich_console__(self, console, options):  # type: ignore[no-untyped-def]
            rule_width = min(display_width, options.max_width)
            yield Text("─" * max(1, rule_width), style="bright_black")

    return _CappedHR


# Headers match inline `code`: rich's defaults give them underlined magenta (h2)
# / bold magenta (h3) etc.; we recolor every level to the same cyan, keeping
# bold (no underline) so headers stay emphasized but in imp's accent rather than
# rich's magenta.
_MD_THEME = {
    "markdown.code": "cyan",
    **{f"markdown.h{n}": "bold cyan" for n in range(1, 8)},
}


def _markdown_to_ansi(text: str, width: int, color: bool, display_width: int = 80) -> str:
    import io

    from rich.console import Console
    from rich.markdown import Markdown
    from rich.theme import Theme
    buf = io.StringIO()
    md = Markdown(text)
    for token in md.parsed:
        if token.type == "inline" and token.children:
            for child in token.children:
                if child.type == "softbreak":
                    child.type = "hardbreak"
    no_bg = _no_bg_code_block_cls()
    md.elements["fence"] = no_bg
    md.elements["code_block"] = no_bg
    md.elements["hr"] = _capped_hr_cls(display_width)
    Console(file=buf, width=width, no_color=not color,
            color_system="standard" if color else None,
            force_terminal=color, soft_wrap=False, highlight=False,
            theme=Theme(_MD_THEME)).print(md)
    return buf.getvalue()


def _render_markdown_block(text: str, width: int, depth: int, color: bool,
                           display_width: int = 80,
                           gutters: tuple[int, ...] = ()) -> list[str]:
    """Render one finalized prose block as markdown→ANSI at inner width, then
    prefix the depth gutter/indent. rich owns bold/heading/list/fence formatting;
    we own the magenta gutter. We never slice rich's output, so no escape is ever
    cut; content rich won't reflow (code fences, tables) is allowed to overflow.

    Theme override: rich's default `markdown.code` is bold+cyan+black-bg. We
    drop the bold and dark bg by setting it to plain `cyan` so inline `code`
    sits calmly in the prose. Headers are recolored to the same cyan, and
    fenced blocks render with no background fill — see `_markdown_to_ansi`."""
    # An agent turn is marked with a leading bold "• " bullet and hang-indented:
    # the bullet sits at the base column and every wrapped/continuation line aligns
    # 2 cols in, under the first word. (A bold dot — heavier than the small
    # "·" chrome separators — so the turn marker clearly leads.) Render markdown 2
    # cols narrower to leave room for the hang, then place the bullet on the first
    # non-blank line and the hang on the rest.
    inner = max(8, width - _INDENT_W * depth - 2)
    indent = _gutter_indent(depth, gutters, color)
    bullet = pal.ansi("• ", "bold", enabled=color)   # bold dot marks the turn
    out: list[str] = []
    first = True
    for raw in _markdown_to_ansi(text, inner, color, display_width).splitlines():
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


def render_question_block(text: str, width: int, depth: int, color: bool,
                          display_width: int = 80,
                          gutters: tuple[int, ...] = ()) -> list[str]:
    inner = max(8, width - _INDENT_W * depth)
    indent = _gutter_indent(depth, gutters, color)
    out: list[str] = []
    for raw in _markdown_to_ansi(text, inner, color, display_width).splitlines():
        stripped = raw.rstrip()
        out.append(indent + stripped if stripped else indent)
    return out or [indent]


def quiet_step_is_silent(node: nv.StepNode) -> bool:
    """A quiet step stays hidden on the happy path (open, a clean linear continue, or
    a silent routing goto) and surfaces its full block only on a non-linear outcome
    (a pause: merge_blocked / retry_ceiling / a max-retries Pause — executor emits
    step_completed with verb="pause"). Shared by `imp log` and the live driver (which
    renders a quiet step as a leaf through this same `_render`), so both paths agree."""
    act = node.outcome.action if (node.outcome and isinstance(node.outcome.action, dict)) else None
    verb = act.get("verb") if act else None
    return verb in (None, "continue", "goto")


def step_hidden(node: nv.StepNode) -> bool:
    """The step's frame (header + breadcrumb) is opted out."""
    return not node.show_label


def step_no_body(node: nv.StepNode) -> bool:
    """The step has no visible body on the happy path: a non-agent step (agents
    always render prose) that is not surfacing its stdout via show_output."""
    return node.kind != "agent" and not node.show_output


def step_has_body(node: nv.StepNode) -> bool:
    """True once the step has recorded any child other than its trailing completion
    breadcrumb — prose, surfaced script output, a lane-wait node, an error, a
    subagent fold, a tool burst. A step whose only child is a breadcrumb (or which
    has no children) has emitted no body."""
    return any(not isinstance(c, nv.BreadcrumbLine) for c in node.children)


def deferred_leaf(node: nv.StepNode) -> bool:
    """A step the renderers hold as a single atomic leaf (blocked until close, then
    rendered in one shot by `_render`) rather than a streamed container. Two cases:
    a HIDDEN chrome step is always a leaf, body-INSENSITIVE (reveal-on-pause parity);
    a VISIBLE body-capable-of-being-empty step is held until it gains a body or
    completes empty (then collapses to one line). `step_no_body` is the outer gate,
    so agents and show_output steps are never held; the live driver calls this
    unchanged, so it flows through both paths."""
    return step_no_body(node) and (step_hidden(node) or not step_has_body(node))


def frame_suppressed(node: Any) -> bool:
    """The node's frame (header + breadcrumb) is hidden. A show_label:false phase
    suppresses unconditionally; a show_label:false step suppresses only on a linear
    outcome — the frame returns on pause/error (reveal-on-error)."""
    if isinstance(node, nv.PhaseNode):
        return not node.show_label
    if isinstance(node, nv.StepNode):
        return step_hidden(node) and quiet_step_is_silent(node)
    return False


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
    tool burst with calls qualifies in either view (lean: a single recap line
    between two prose turns; full: an expanded fold that wants air on both
    sides). Under ``full`` the everything-view also spaces the config-dump
    (``StepContextNode``) and expanded ``thinking`` (``ThinkingFold``) so unlike
    block kinds don't bunch. Structural chrome — a phase header/complete line, a
    step (which ends on its completion breadcrumb), and errors — is surrounded by
    blank lines so each stands clear of the prose around it. The batch walk and
    the live driver both key spacing off this, so they stay identical
    (StepNode/PhaseNode matter only to the batch walk, which treats a container
    as one child; live keys off the last leaf — the breadcrumb — instead, and
    they agree). A node that renders nothing (whitespace-only thinking) still
    leaves adjacency untouched: `_render_children` skips empty emissions before
    this value is consulted, so a True here on an empty render is inert."""
    return bool(
        is_prose_leaf(node)
        or isinstance(node, (nv.UserMessageNode, nv.SkillLoadNode, nv.SubagentFold,
                             nv.PhaseNode, nv.StepNode, nv.BreadcrumbLine,
                             nv.ErrorNode, nv.PauseNode, nv.RenameNode))
        or (isinstance(node, nv.ToolBurstFold) and node.calls)
        or (full and isinstance(node, (nv.ThinkingFold, nv.StepContextNode)))
    )


def spaces_before(node: Any) -> bool:
    """A node that wants a blank line ABOVE it regardless of what preceded it.
    An interactive step (the human's turn is a conversational beat), a phase
    header (a new phase starts on its own), and an error (it must stand clear of
    the line before it) all qualify. A regular step-completion breadcrumb is NOT
    here: it already trails a spaced block (the step's final prose / the header
    gap), so it gets its blank-above for free without risking a doubled separator.
    The batch walk and the live driver both key off this so the lead-in blank
    lands identically in `imp log` and the live view."""
    return bool(
        (isinstance(node, nv.StepNode) and node.interactive)
        or isinstance(node, (nv.PhaseNode, nv.ErrorNode, nv.PauseNode, nv.RenameNode,
                             nv.MergedNode, nv.WarningsSummaryNode, nv.CompletedNode))
    )


def _render_children(children: list[Any], *, depth: int, full: bool, color: bool,
                     width: int, display_width: int = 80, gutters: tuple[int, ...] = (),
                     prime: bool = False, live_viewer: bool = False) -> Iterator[str]:
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
                             display_width=display_width, gutters=gutters,
                             live_viewer=live_viewer))
        if not lines:
            continue
        if prev_prose or spaces_before(child):
            yield _gutter_indent(depth, gutters, color)
        yield from lines
        prev_prose = spaces_after(child, full=full)


def compose_label(segments: Iterable[str | None]) -> str:
    """Join the present (truthy) segments with ` · `."""
    return " · ".join(s for s in segments if s)


def join_space(*segments: str | None) -> str | None:
    present = [s for s in segments if s]
    return " ".join(present) if present else None


def _phase_rule(label: str) -> str:
    """Fixed-width by design — the navigational landmark is the yellow
    `lifecycle` color, not a full-width bar."""
    return f"── {label} ───"


def phase_breadcrumb_line(node: nv.PhaseNode, *, depth: int, color: bool,
                          gutters: tuple[int, ...] = ()) -> str:
    """The quiet gray `<phase> phase complete · <N>s` end line, shared by both renderers (§7).
    `depth` is the phase HEADER's depth — pass `depth` from render_text and
    `cur.depth` from the live driver; both equal the header level.
    This depth equivalence holds only because phases appear solely as direct
    children of the root (single-level invariant) and would break if phases
    ever nested inside SubagentFolds."""
    elapsed = nv.phase_elapsed(node)
    done = fmt_seconds_done(elapsed)
    secs = f" · {done}" if done else ""
    label = node.label or f"{node.name} phase"
    return _line(f"{label} complete{secs}", depth, color,
                 *pal.NODE_COLORS["breadcrumb"], gutters=gutters)


def _step_header_label(node: nv.StepNode) -> str:
    """The step's own scrollback label — its position counter (live or static) for
    a `position_label` step, else its plain label. The raw step name is the loud
    fallback when a visible step somehow has no label, so the omission is
    caught and fixed. This helper returns a phase-free label; both
    `collapsed_step_line` and `header_line` prepend the owning phase's label (via
    `compose_label`) to this result."""
    step_label = (node.live_label or node.label) if node.position_label else node.label
    return step_label or node.name


def collapsed_step_line(node: nv.StepNode, *, depth: int, color: bool,
                        gutters: tuple[int, ...] = ()) -> str:
    """The single line a body-less visible step collapses to on completion:
    `<label> · complete · <elapsed>` — label in the step color, the
    ` · complete · <elapsed>` tail in the existing breadcrumb gray."""
    label = compose_label([node.phase_label, _step_header_label(node)])
    done = fmt_seconds_done(nv.elapsed_seconds(node.run_start_ts or node.start_ts, node.end_ts))
    tail = " · complete" + (f" · {done}" if done else "")
    indent = _gutter_indent(depth, gutters, color)
    return (indent
            + pal.ansi(label, *pal.NODE_COLORS["step"], enabled=color)
            + pal.ansi(tail, *pal.NODE_COLORS["breadcrumb"], enabled=color))


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
        return _line(_phase_rule(node.label or f"{node.name} phase"), depth, color,
                     *pal.NODE_COLORS["lifecycle"], gutters=gutters)
    if isinstance(node, nv.SubagentFold):
        return _line(f"· {node.subagent_name}", depth, color,
                     *pal.NODE_COLORS["subagent"], gutters=gutters)
    label = _step_header_label(node)
    # The printed scrollback header prepends the owning phase's label (when set),
    # so it reads e.g. `implementation phase · plan 1/1 · task 3/3`; compose_label
    # drops a None phase_label, falling back to the step label alone.
    label = compose_label([node.phase_label, label])
    route = (pal.ansi(f" · {node.route_label}", *pal.NODE_COLORS["lifecycle"],
                      enabled=color)
             if node.route_label else "")
    if not node.resumed:
        head = pal.ansi(label, *pal.NODE_COLORS["step"], enabled=color)
        if node.model:
            # Gray to match the footer's model segment.
            head += pal.ansi(f" · {node.model}", "gray", enabled=color)
        return _gutter_indent(depth, gutters, color) + head + route
    indent = _gutter_indent(depth, gutters, color)
    parts = [pal.ansi(label, *pal.NODE_COLORS["step"], enabled=color),
             pal.ansi(" (resume)", "gray", enabled=color)]
    if node.model:
        parts.append(pal.ansi(f" · {node.model}", "gray", enabled=color))
    return indent + "".join(parts) + route


def subagent_model(sub: nv.SubagentFold) -> str | None:
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
    abnormal terminal state (running-at-pause, failed, stalled, interrupted, context exhausted)
    appends a plain `(state)` tag so a subagent that didn't cleanly finish still
    stands out."""
    sub_c = pal.NODE_COLORS["subagent"]
    parts = [pal.ansi(f"  {sub.subagent_name}", *sub_c, enabled=color)]
    model = subagent_model(sub)
    if model:
        # Gray, like the step header's model.
        parts.append(pal.ansi(f" · {model}", "gray", enabled=color))
    if sub.running:
        parts.append(pal.ansi(" (running)", *sub_c, enabled=color))
    elif sub.terminal_state == "stalled":
        parts.append(pal.ansi(" (stalled)", *sub_c, enabled=color))
    elif sub.terminal_state == "interrupted":
        parts.append(pal.ansi(" (interrupted)", *sub_c, enabled=color))
    elif sub.terminal_state == "context_exhausted":
        parts.append(pal.ansi(" (context exhausted)", *sub_c, enabled=color))
    elif sub.is_error:
        parts.append(pal.ansi(" (failed)", *sub_c, enabled=color))
    elapsed = nv.elapsed_seconds(sub.start_ts, sub.end_ts)
    done = "" if sub.running else fmt_seconds_done(elapsed)
    if done:
        parts.append(pal.ansi(f" · {done}", "gray", enabled=color))
    return _gutter_indent(depth, gutters, color) + "".join(parts)


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
        # A plain-string SUCCESS return is exactly the nested transcript's final
        # prose turn — echoing it here double-prints it (§4). Surface a non-dict
        # outcome only on the error path, where there may be no final prose turn.
        if not sub.is_error:
            return
        yield from _wrap(_caret_escape(raw), width, depth, color, *codes, gutters=gutters)
        return
    scalars: list[str] = []
    blocks: list[tuple[str, str]] = []
    for k, v in data.items():
        if isinstance(v, str) and "\n" in v:
            blocks.append((k, v))
        else:
            scalars.append(_caret_escape(f"{k}={v}"))
    if scalars:
        yield _line("→ " + " · ".join(scalars), depth, color, *codes, gutters=gutters)
    for k, v in blocks:
        yield _line(_caret_escape(f"{k}:"), depth, color, *codes, gutters=gutters)
        yield from _wrap(_caret_escape(v), width, depth + 1, color, *codes, gutters=gutters)


def _render(node: Any, *, depth: int, full: bool, color: bool,
            width: int, display_width: int = 80, gutters: tuple[int, ...] = (),
            live_viewer: bool = False) -> Iterator[str]:
    if isinstance(node, nv.PhaseNode):
        # The phase start line is surrounded by blank lines: the blank ABOVE comes
        # from the root walk (spaces_before(PhaseNode)); the blank BELOW comes from
        # priming the children walk so the first step gets a lead-in blank.
        suppress = frame_suppressed(node)
        if not suppress:
            yield header_line(node, depth=depth, color=color, gutters=gutters)
        yield from _render_children(node.children, depth=depth, full=full,
                                    color=color, width=width, display_width=display_width,
                                    gutters=gutters, prime=True, live_viewer=live_viewer)
        if not suppress and node.closed and nv.phase_elapsed(node) is not None:
            # Blank ABOVE the phase-complete line (it's surrounded too); the blank
            # BELOW comes from the root walk (spaces_after(PhaseNode)).
            yield _gutter_indent(depth, gutters, color)
            yield phase_breadcrumb_line(node, depth=depth, color=color, gutters=gutters)
        return

    if isinstance(node, nv.StepNode):
        if deferred_leaf(node) and quiet_step_is_silent(node):
            if step_hidden(node):
                return                       # hidden chrome: silent on a linear outcome
            if node.closed:
                yield collapsed_step_line(node, depth=depth, color=color,
                                          gutters=gutters)
                return
            # A visible body-less step still open at render time (a force-flush at
            # run end, or a truncated log) falls through to render just its header —
            # an in-flight step must not be labelled "complete".
        # A suppressed-frame step that yielded to a non-linear outcome (a pause/error) falls
        # through here and renders its full block, just like a loud step.
        # A step's content flows at the step's OWN indent (no +1): the header is a
        # label and its prose/breadcrumb align directly beneath it, rather than
        # indenting a further level and burying the agent's words. Phases no longer
        # indent their steps either — the transcript is flat at column 0; subagent
        # folds add their own level + gutter internally. (design: aligned indent)
        suppress = frame_suppressed(node)
        if not suppress:
            yield header_line(node, depth=depth, color=color, gutters=gutters)
        # One blank line between the step header and the agent's first words, so the
        # speaker label and the speech don't bunch (design: §5.4 air). Priming the
        # children walk (rather than emitting the blank here) means a first child
        # that ALSO wants a blank above — an error — gets a single separator, not a
        # doubled one.
        children = ([c for c in node.children if not isinstance(c, nv.BreadcrumbLine)]
                    if suppress else node.children)
        yield from _render_children(children, depth=depth, full=full,
                                    color=color, width=width, display_width=display_width,
                                    gutters=gutters, prime=True, live_viewer=live_viewer)
        if full and node.agent_output:
            yield from _render_output_args(node.agent_output, depth=depth,
                                           color=color, width=width, gutters=gutters)
        return

    if isinstance(node, nv.AgentTextNode):
        yield from _render_markdown_block(_caret_escape(node.text), width, depth,
                                          color, display_width, gutters)
        return

    if isinstance(node, nv.LiteralTextNode):
        if node.verbatim:
            codes = pal.NODE_COLORS[node.color_role] if node.color_role else ()
            for line in _caret_escape(node.text).split("\n"):
                yield _line(line, depth, color, *codes, gutters=gutters)
            return
        if node.color_role:
            yield from _wrap(_caret_escape(node.text), width, depth, color,
                             *pal.NODE_COLORS[node.color_role], gutters=gutters)
            return
        yield from _render_markdown_block(_caret_escape(node.text), width, depth,
                                          color, display_width, gutters)
        return

    if isinstance(node, nv.UserMessageNode):
        if not node.interactive:
            # A non-interactive message is an injected prompt, not a human turn:
            # the top-level agent's real instructions, or (inside a fold) the
            # parent's task_description to a subagent. Lean/live keep dropping it;
            # --full surfaces it as a dim `injected message:` labeled verbatim
            # block, mirroring the step_context metadata dump. Body is text, not
            # markdown — it is a verbatim prompt. An empty/whitespace-only body
            # renders nothing, leaving adjacency state untouched.
            if not (full and node.text.strip()):
                return
            yield _line("injected message:", depth, color,
                        *pal.NODE_COLORS["metadata"], gutters=gutters)
            yield from _wrap(_caret_escape(node.text), width, depth + 1, color,
                             *pal.NODE_COLORS["metadata"], gutters=gutters)
            return
        # A brand-orange `>` caret marks the human turn on the first line only; it
        # sits OUTSIDE the slate. The separating space + the wrapped text sit ON the
        # slate so the band hugs the text. Continuation lines carry a blank caret
        # column (no repeated caret) and align under the first line's text (two
        # columns past the caret), with the slate band continuing.
        #
        # On a truecolor theme the reprinted (scrolled-back) turn carries a subtle
        # entered-input background, opened AFTER the caret column so the highlight
        # covers the separating space + the typed text and closes at text end (no
        # full-width fill → clean copy/paste). Blank separator lines stay unpainted.
        # The live input field is painted by prompt.py, not here.
        indent = _gutter_indent(depth, gutters, color)
        caret = pal.ansi(">", "wordmark", enabled=color)
        bg = pal.user_bg_open(enabled=color)
        end = "\x1b[0m" if bg else ""
        avail = max(8, width - _INDENT_W * depth - 2)
        first = True
        for para in _caret_escape(node.text).split("\n"):
            if not para:
                yield indent
                continue
            for line in textwrap.wrap(para, width=avail) or [""]:
                lead = caret if first else " "
                yield indent + lead + bg + " " + line + end
                first = False
        return

    if isinstance(node, nv.ThinkingFold):
        if not node.content.strip():
            return  # blank/whitespace thinking is pure noise — drop in both modes
        if full:
            yield _line(f"{pal.fold_glyph(expanded=True)} thinking", depth, color,
                        *pal.NODE_COLORS["thinking"], gutters=gutters)
            yield from _wrap(_caret_escape(node.content), width, depth + 1, color,
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
                                    display_width=display_width, gutters=gutters,
                                    live_viewer=live_viewer)
        return

    if isinstance(node, nv.StepContextNode):
        yield from _render_step_context(node, depth=depth, full=full, color=color, width=width,
                                        gutters=gutters, live_viewer=live_viewer)
        return

    if isinstance(node, nv.OutcomeLine):
        text = pal.action_text(node.action)
        yield _line(f"→ {text}", depth, color, *pal.NODE_COLORS["lifecycle"], gutters=gutters)
        return

    if isinstance(node, nv.BreadcrumbLine):
        name = node.step_label or node.step_name
        act = node.action if isinstance(node.action, dict) else None
        blocked = (node.kind == "merge_and_exit_worktree" and act is not None
                   and act.get("verb") == "pause")
        parts: list[str | None]
        if node.interrupted:
            parts = [f"{name} interrupted"]
        elif blocked:
            parts = [f"{name} blocked"]
        else:
            parts = [f"{name} · complete" if node.show_complete else name]
        parts.insert(0, node.phase_label)
        if full:
            parts.extend(_caret_escape(e) for e in node.extras)
        if not (node.interrupted or blocked) and (act is None or act.get("verb") != "goto"):
            verb_text = pal.action_text(node.action) if node.action is not None else None
            if verb_text and verb_text != "continue":
                parts.append(verb_text)
        done = fmt_seconds_done(node.elapsed_s)
        if done:
            parts.append(done)
        # Route reason rides the next step's header, not a line beneath the completion.
        yield _line(compose_label(parts), depth, color,
                    *pal.NODE_COLORS["breadcrumb"], gutters=gutters)
        return

    if isinstance(node, nv.PauseNode):
        glyph = "⏸ " if color else ""
        copy = _PAUSE_REASON_COPY.get(node.reason)
        text = f"paused — {copy}" if copy else f"paused ({node.reason})"
        yield _line(f"{glyph}{text}", depth, color,
                    *pal.NODE_COLORS["lifecycle"], gutters=gutters)
        return

    if isinstance(node, nv.RenameNode):
        # A naming reads as a quiet agent-style aside, inline where it happened:
        # a wordmark "• " turn bullet, then "Set name to" with the new title also in
        # wordmark — the warm accent — so the whole aside reads as one identity signal.
        indent = _gutter_indent(depth, gutters, color)
        bullet = pal.ansi("• ", "wordmark", enabled=color)
        name = pal.ansi(_caret_escape(node.name), "wordmark", enabled=color)
        yield indent + bullet + 'Set name to "' + name + '"'
        return

    if isinstance(node, nv.ErrorNode):
        if node.is_warning:
            yield _line(f"warning: {_caret_escape(node.message)}", depth, color,
                        *pal.NODE_COLORS["warning"], gutters=gutters)
        else:
            tag = f"[{node.category}] " if node.category else ""
            yield _line(f"error: {tag}{_caret_escape(node.message)}", depth, color,
                        *pal.NODE_COLORS["error"], gutters=gutters)
            is_script = node.category == "script"
            if (full or is_script) and node.detail and node.detail not in node.message:
                detail = node.detail
                if is_script and not full:
                    lines = detail.split("\n")
                    if len(lines) > _SCRIPT_DETAIL_TAIL_LINES:
                        hidden = len(lines) - _SCRIPT_DETAIL_TAIL_LINES
                        noun = "line" if hidden == 1 else "lines"
                        yield _line(f"… ({hidden} earlier {noun} — imp log --full)",
                                    depth + 1, color, *pal.NODE_COLORS["metadata"],
                                    gutters=gutters)
                        detail = "\n".join(lines[-_SCRIPT_DETAIL_TAIL_LINES:])
                yield from _wrap(_caret_escape(detail), width, depth + 1, color,
                                 *pal.NODE_COLORS["error"], gutters=gutters)
        return

    if isinstance(node, nv.SkillLoadNode):
        # A loaded skill, shown once as a quiet `loading skill {name}` line (the
        # redundant `load_skill` tool call is dropped from the burst recap). No
        # checkmark — indented two cols like the other sub-detail chrome.
        yield _line(f"  loading skill {node.name}", depth, color,
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

    if isinstance(node, nv.WarningsSummaryNode):
        mark = "⚠ " if color else ""
        noun = "warning" if node.total == 1 else "warnings"
        yield _line(f"{mark}{node.total} {noun} this run", depth, color,
                    *pal.NODE_COLORS["warning"], gutters=gutters)
        for message, count in node.lines:
            suffix = f"  (×{count})" if count > 1 else ""
            yield _line(f"  • {_caret_escape(message)}{suffix}", depth, color,
                        *pal.NODE_COLORS["warning"], gutters=gutters)
        return

    if isinstance(node, nv.CompletedNode):
        mark = "✓ " if color else ""
        yield _line(f"{mark}Completed {node.change_id}.", depth, color,
                    *pal.NODE_COLORS["lifecycle"], gutters=gutters)
        return

    if isinstance(node, nv.MergedNode):
        mark = "✓ " if color else ""
        yield _line(f"{mark}Merged {node.change_id} into {node.base}.", depth, color,
                    *pal.NODE_COLORS["lifecycle"], gutters=gutters)
        return

    if isinstance(node, nv.FallbackLine):
        if not full:
            # An event with no curated rendering is engine bookkeeping
            # (vars_backfilled, workflow_refreshed, …) — raw `type k=v` noise in
            # the lean/live view; it survives in imp log --full below.
            return
        kv = " ".join(f"{k}={_caret_escape(str(v))}" for k, v in node.payload.items())
        yield _line(f"{node.event_type}  {kv}".rstrip(), depth, color,
                    *pal.NODE_COLORS["breadcrumb"], gutters=gutters)
        return


_ARG_VALUE_CLIP = 120        # §2: per-arg-value clip on the --full args line
_OUTPUT_LINE_CLIP = 400      # §2: per-source-line clip in the --full output block
_SUBAGENT_TASK_CLIP = 60     # §3: per-header clip on the --full subagent task description
_OUTPUT_MAX_LINES = 10       # §2: source lines shown before the "+N more" tail
_SCRIPT_DETAIL_TAIL_LINES = 15   # lean view: last N lines of a blocked-script detail


def _call_args_line(c: nv.ToolCall) -> str:
    """`· {name}[ tag]  k=v …` with each value newline-collapsed and clipped."""
    if c.killed:
        tag = " [killed]"                 # killed wins; tags never stack
    elif c.is_error:
        tag = " [error]"
    else:
        tag = ""
    parts = []
    for k, v in (c.input or {}).items():
        val = _caret_escape(" ".join(str(v).split()))   # collapse, then neutralize
        if len(val) > _ARG_VALUE_CLIP:
            val = val[:_ARG_VALUE_CLIP] + "…"
        parts.append(f"{k}={val}")
    head = f"· {nv.display_tool_name(c.name)}{tag}"
    return f"{head}  {'  '.join(parts)}".rstrip() if parts else head


def _burst_summary(burst: nv.ToolBurstFold) -> str | None:
    """One-line recap of a finished tool burst for the lean view: the distinct
    tool names (first-seen order, capped), the call count, and the burst
    duration. The duration mirrors the live status timer's prose → prose span
    (the heads-down interval between agent outputs), falling back to the
    first-call → last-result tool span when a burst has no surrounding prose, so
    the recap equals what the live timer read the instant the burst closed.
    Two names are omitted: `load_skill` (it renders as its own
    `loading skill …` line, so counting it here would double up) and the bare `tool`
    placeholder (the sentinel name for an orphan/unnamed result — no real tool info).
    Returns None when nothing renderable remains."""
    calls = nv.visible_calls(burst)
    if not calls:
        return None
    names = list(dict.fromkeys(nv.display_tool_name(c.name) for c in calls))
    shown = " · ".join(names[:4])
    more = f" +{len(names) - 4}" if len(names) > 4 else ""
    n = len(calls)
    count = f"{n} call{'s' if n != 1 else ''}"
    elapsed = nv.elapsed_seconds(burst.span_start_ts or burst.start_ts,
                                 burst.span_end_ts or burst.end_ts)
    done = fmt_seconds_done(elapsed)
    secs = f" · {done}" if done else ""
    # Count-first (§3): the count leads, then the distinct names, then duration.
    # No leading marker — a tool burst is sub-detail of the agent's turn and is
    # indented (by _render_burst) under the turn's words. Internal `·` stay.
    return f"{count} · {shown}{more}{secs}"


def _render_output_args(args: dict[str, Any], *, depth: int, color: bool,
                        width: int, gutters: tuple[int, ...] = ()) -> Iterator[str]:
    prose = [(k, v) for k, v in args.items()
             if nv.is_prose_field(k, v) and v.strip()]
    if not prose:
        return
    yield _line(f"{pal.fold_glyph(expanded=True)} complete_step", depth, color,
                *pal.NODE_COLORS["tools"], gutters=gutters)
    for k, v in prose:
        yield from _wrap(f"{k}: {_caret_escape(v)}", width, depth + 1, color,
                         *pal.NODE_COLORS["tools"], gutters=gutters)


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
    if not burst.calls:
        return                            # §1 empty-burst guard (split may empty it)
    yield _line(f"{pal.fold_glyph(expanded=True)} tools", depth, color,
                *pal.NODE_COLORS["tools"], gutters=gutters)
    for c in burst.calls:
        yield from _hang_wrap(_call_args_line(c), "", width, depth + 1, color,
                              *pal.NODE_COLORS["tools"], gutters=gutters)
        result = c.result or ""
        if result:
            src = _caret_escape(result).split("\n")
            shown = src[:_OUTPUT_MAX_LINES]
            clipped = [(ln[:_OUTPUT_LINE_CLIP] + "…") if len(ln) > _OUTPUT_LINE_CLIP
                       else ln for ln in shown]
            block = "\n".join(clipped)
            yield from _hang_wrap(block, "→ ", width, depth + 1, color,
                                  *pal.NODE_COLORS["tools"], gutters=gutters)
            extra = len(src) - len(shown)
            if extra > 0:
                yield from _hang_wrap(f"… (+{extra:,} more lines)", "→ ", width,
                                      depth + 1, color, *pal.NODE_COLORS["tools"],
                                      gutters=gutters)
        if c.citations_footer:
            # Footer rides outside the clip; render whether or not an output block
            # printed. When result is non-empty, align at the "→ " column; when
            # empty (footer-only shape), render without the arrow anchor so the
            # output reads cleanly without a stray "→ " prefix.
            footer = _caret_escape(c.citations_footer).lstrip("\n")
            if result:
                yield from _hang_wrap(footer, "→ ", width, depth + 1, color,
                                      *pal.NODE_COLORS["tools"], gutters=gutters)
            else:
                yield from _wrap(footer, width, depth + 1, color,
                                 *pal.NODE_COLORS["tools"], gutters=gutters)


def _render_subagent(sub: nv.SubagentFold, *, depth: int, full: bool,
                     color: bool, width: int, display_width: int = 80,
                     gutters: tuple[int, ...] = (),
                     live_viewer: bool = False) -> Iterator[str]:
    if not full:
        # Lean: ONE status row, no children, no outcome (Part D). Structurally
        # eliminates the SDK cancellation-sentinel leak — lean renders no subagent
        # I/O at all, only `· {name}` (plus elapsed / abnormal-state tag).
        yield subagent_status_row(sub, depth=depth, color=color, gutters=gutters)
        return
    # --full: expanded `▾ {name}` fold header, full recursion of the subagent's
    # narrative (nested thinking/tools expanded), then the structured return value.
    # --full stays a superset of the lean view.
    header = f"{pal.fold_glyph(expanded=True)} {sub.subagent_name}"
    task = " ".join(sub.task_description.split()) if sub.task_description else ""
    if task:
        if len(task) > _SUBAGENT_TASK_CLIP:
            task = task[:_SUBAGENT_TASK_CLIP] + "…"
        header += f" · {_caret_escape(task)}"
    yield _line(header, depth, color, *pal.NODE_COLORS["subagent"], gutters=gutters)
    yield from _render_children(sub.tree.root.children, depth=depth + 1, full=full,
                                color=color, width=width, display_width=display_width,
                                gutters=(*gutters, depth), live_viewer=live_viewer)
    yield from subagent_outcome_lines(sub, depth=depth + 1, color=color, width=width,
                                      gutters=(*gutters, depth))


def _render_step_context(ctx: nv.StepContextNode, *, depth: int, full: bool,
                         color: bool, width: int,
                         gutters: tuple[int, ...] = (),
                         live_viewer: bool = False) -> Iterator[str]:
    if live_viewer:
        return
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
    yield from _wrap(_caret_escape(str(p.get("system_prompt", ""))), width, depth + 2,
                     color, *pal.NODE_COLORS["metadata"], gutters=gutters)
    yield _line("tools:", depth + 1, color, *pal.NODE_COLORS["metadata"], gutters=gutters)
    for t in tools:
        name = t.get("name", t) if isinstance(t, dict) else t
        yield _line(f"- {_caret_escape(str(name))}", depth + 2, color,
                    *pal.NODE_COLORS["metadata"], gutters=gutters)
