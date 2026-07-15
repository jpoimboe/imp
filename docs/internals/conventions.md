# imp conventions

Read this when working on imp's terminal output or any user-facing copy.

## Visual identity

imp keeps its own style — familiar in spirit to other coding CLIs, not a copy. The prompt caret
settled to a plain `>`. Prefer plainer, imp-native glyphs over borrowed ones.
When proposing a visual change, offer an imp-native option and flag any borrowed
conventions so the choice is deliberate rather than inherited.

## Plain-speak copy

User-facing copy uses only imp-canon nouns: quest, workflow, backlog, phase,
step, spec, plan. Coined brainstorm terms (e.g. "express") may exist only as
internal engine values — never user-facing. The runtime agents `threshold` and
`finalize` already self-enforce plain terms; this doc is the canonical reference
for new copy, so no agent edits are needed to honor it.

## Seeing imp's own UI

When the project under change is imp itself, `scripts/capture_imp_ui.py` renders
imp's own TUI from fixtures — the cheap, deterministic way to see the real
appearance without a live run (its human companion is `scripts/snap_imp_ui.py`).
This is the imp-specific home for that knowledge; the project-agnostic manual for
driving *any* terminal program is the `imp-driving-terminal-uis` skill.

## Trailing output spacing

Human-facing subcommand output ends with exactly one trailing blank line when
stdout is a terminal (`sys.stdout.isatty()`) — never zero, never two; existing
trailing blanks collapse to one. Route list-building commands through
`_page_or_print(..., trailing_blank=True)` or `_emit_human(lines)`, both of which
reuse `_with_trailing_blank`. Piped/redirected output and machine formats
(`--json`, `log --html/--md/-`, `wrote <path>` confirmations) stay byte-clean —
no added blank line. New human subcommands must follow this rule.

---

*Part of imp's self-knowledge corpus. Runtime entry point: load the `imp-knowledge` skill (`load_skill(name="imp-knowledge")`), the thin index that maps into this corpus.*
