imp is pre-release with no external users. Skip backward-compatibility entirely
in designs and plans — no migration paths, no state.json/event-payload
fallbacks for old versions, no dataclass defaults added only to read older
files (DO preserve invariants within a single in-flight run). Design docs are
point-in-time: write each self-contained; don't reconcile or update superseded
specs.

When working on imp's TUI, output, or user-facing copy, read
docs/internals/conventions.md. When editing imp's engine/renderer or workflow
prompts, read docs/internals/invariants.md. When editing imp's skills or docs,
read docs/internals/knowledge-map.md.

When changing a tool/CLI interface, update the affected agent/skill/doc files
in the same change. `imp-knowledge` is an agent-facing skill — imp's canonical
self-knowledge index that agents load, not human docs. Keep it accurate
whenever you change imp's CLI commands/flags, agent tools, or core
concepts/vocabulary. When editing agent/skill prose, be maximally succinct —
every token counts. Before changing user-facing copy or agent prompt text, get
the user's approval on exact wording, showing before/after.
