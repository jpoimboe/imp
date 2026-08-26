# imp conventions

Read this when working on imp's terminal output or any user-facing copy. Code
style is a separate layer — see **Code comments** below.

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

## Code comments

Comment and docstring style is coding stance, not copy, so the operative rule
lives in the shared partial `src/imp/assets/partials/coding-stance.md` (overlaid
into every quest as `workflow/partials/coding-stance.md`) and the coding agents
already carry it. The short form: default to no comments, add one only when the
WHY is non-obvious, and never write multi-paragraph docstrings or multi-line
comment blocks — one short line max. To change the rule, edit the partial; this
section only says where it lives.

## Seeing imp's own UI

When the project under change is imp itself, `scripts/capture_imp_ui.py` renders
imp's own TUI from fixtures — the cheap, deterministic way to see the real
appearance without a live run (its human companion is `scripts/snap_imp_ui.py`).
This is the imp-specific home for that knowledge; the project-agnostic manual for
driving *any* terminal program is the `imp-driving-terminal-uis` skill. Its web
sibling — rendering and driving a page in a browser — is `imp-driving-web-uis`.

## Trailing output spacing

Human-facing subcommand output ends with exactly one trailing blank line when
stdout is a terminal (`sys.stdout.isatty()`) — never zero, never two; existing
trailing blanks collapse to one. Route list-building commands through
`_page_or_print(..., trailing_blank=True)` or `_emit_human(lines)`, both of which
reuse `_with_trailing_blank`. Piped/redirected output and machine formats
(`--json`, `log --html/--md/-`, `wrote <path>` confirmations) stay byte-clean —
no added blank line. New human subcommands must follow this rule.

## Error and diagnostic output

imp routes error and diagnostic surfaces through two shared sinks — no surface
styles its own line.

- **Quest-scoped terminal failures → the exit block.** A failure that identifies
  a quest (agent error, merge blocked, session-daemon startup failure) renders as
  the gold three-part `_error_block` — red head + body + gold identity line — on
  **stdout**, via `_emit_merge_blocked` / `_emit_startup_failed`. The exit code
  carries the error signal for scripts.
- **Non-quest CLI usage/lookup errors → one `imp:` line.** Bad arguments, no such
  quest, unknown command, and similar usage errors emit a single
  `imp: <message>` line on **stderr** via `_emit_cli_error`, which centralizes the
  prefix so no CLI call site hand-rolls it.

Carved out (unchanged): rc-conditional proxy-response relays that print a
pre-formatted `imp: …` message routed by return code (`_emit_mutation_result`,
`_cmd_edit`, the `_async_cmd_new` background path, `_abandon`); the detached-HEAD
notice `imp new` / `imp chat` print on stdout via `_emit_human`; the SIGTERM
success line; `warmup.py`'s `imp: warmup skipped …` lines, which reach the
foreground terminal too now that `init` / `new` / `chat` / `resume` warm the
module graph in-process; and daemon-subprocess-internal diagnostics (executor
traceback, `daemon.py`, the archive progress indicator, `loader.py` warnings,
`validate_memory_cli.py`, `daemon_connector.py`) — a raw Python traceback stays
raw to keep its diagnostic value, reaching the user via the crash-banner
mechanism.

## See also

- `internals/architecture.md` — the engine and renderer these surfaces are built on.
- `internals/invariants.md` — the layering rules that constrain renderer changes.
- `guide/introspection.md` — reading a quest's live state rather than styling it.
- The full corpus index is the `imp-knowledge` skill's *More depth* section.

---

*Part of imp's self-knowledge corpus. Runtime entry point: load the `imp-knowledge` skill (`load_skill(name="imp-knowledge")`), the thin index that maps into this corpus.*
