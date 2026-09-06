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

When changing a tool/CLI interface, update the affected agent/skill/doc files
in the same change. `imp-knowledge` is an agent-facing skill — imp's canonical
self-knowledge index that agents load, not human docs. Keep it accurate
whenever you change imp's CLI commands/flags, agent tools, or core
concepts/vocabulary. When editing agent/skill prose, be maximally succinct —
every token counts.

Flaky tests under load are a major problem. Write tests to tolerate
timing/load variance: poll for the actual signal instead of fixed sleeps, give
inner waits generous budgets that never false-fire under saturated `-n auto`
CI, and rely on a single outer timeout to catch genuine hangs.

imp's git history was squashed to a single `imp: initial commit`; the complete
pre-squash history lives in a separate clone at `~/imp-orig`. For any git
archaeology — resolving a commit SHA from a bug report, tracing when a prompt
or partial changed — run git there (e.g. `git -C ~/imp-orig log -S<string> --
<path>`), since those commits are unreachable in the working checkout.
