# imp conventions

Read this when working on imp's terminal output or any user-facing copy.

## Visual identity

imp keeps its own style — Claude-like in spirit, not a copy. The prompt caret
settled to a plain `>`. Prefer plainer, imp-native glyphs over borrowed ones.
When proposing a visual change, offer an imp-native option and flag any Claude
borrowings so the choice is deliberate rather than inherited.

## Plain-speak copy

User-facing copy uses only imp-canon nouns: Quest, workflow, backlog, phase,
step, spec, plan. Coined brainstorm terms (e.g. "express") may exist only as
internal engine values — never user-facing. The runtime agents `threshold` and
`finalize` already self-enforce plain terms; this doc is the canonical reference
for new copy, so no agent edits are needed to honor it.

## Seeing imp's own UI

When the project under change is imp itself, `scripts/capture_imp_ui.py` renders
imp's own TUI from fixtures — the cheap, deterministic way to see the real
appearance without a live run (its human companion is `scripts/snap_imp_ui.py`).
This is the imp-specific home for that knowledge; the project-agnostic manual for
driving *any* terminal program is the `driving-terminal-uis` skill.
