imp is pre-release with no external users. Skip backward-compatibility entirely
in designs and plans — no migration paths, no state.json/event-payload
fallbacks for old versions, no dataclass defaults added only to read older
files (DO preserve invariants within a single in-flight run). Design docs are
point-in-time: write each self-contained; don't reconcile or update superseded
specs.

When working on imp's TUI, output, or user-facing copy, read
docs/imp-conventions.md. When editing imp's engine/renderer or workflow
prompts, read docs/imp-invariants.md.
