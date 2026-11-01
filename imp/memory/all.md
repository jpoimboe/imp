imp is in production: existing quests and their on-disk data must survive
format changes. When you change a persisted format (event log, state.json,
archive blobs), ship a one-shot migration that upgrades existing data in place
— live quest homes and the archive both — rather than reader-side version
fallbacks; keep readers single-version. Preserve invariants within a single
in-flight run.

When working on imp's TUI, output, or user-facing copy, read
docs/internals/conventions.md. When editing imp's engine/renderer or workflow
prompts, read docs/internals/invariants.md. When editing imp's skills or docs,
read docs/internals/knowledge-map.md.

`imp-knowledge` is an agent-facing skill — imp's canonical
self-knowledge index that agents load, not human docs. Keep it accurate
whenever you change imp's CLI commands/flags, agent tools, or core
concepts/vocabulary. When editing agent/skill prose, be maximally succinct —
every token counts.
