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

## Commit messages

Commit message style is coding stance too, so the operative rule lives in the
shared partial `src/imp/assets/partials/commit-style.md` (overlaid into every
quest as `workflow/partials/commit-style.md`) and the committing agents already
carry it. The short form: a `component: imperative summary` subject under 100
characters naming the part of the codebase that changed, and a required body
wrapped at 75 columns in three movements — context, consequence, the fix —
written to a file and committed with `git commit -F`, never `-m`. To change the
rule, edit the partial; this section only says where it lives.

The merge commit is the exception: no agent writes it. The engine assembles it
in `_merge_subject()` (`src/imp/engine/worktree.py`): the subject is the quest
title verbatim — no `Merge ` prefix, since merge-ness is structural (`git log
--merges`) and provenance is the `Quest: <id>` trailer (`git log
--grep='^Quest:'`) — followed by the leading paragraph of the summarizer's
`artifacts/summary/<id>.md` (extracted by `_summary_body()` in
`src/imp/engine/executor.py`) and that trailer. A quest whose summary artifact
is missing merges with the subject and trailer alone.

## Seeing imp's own UI

When the project under change is imp itself, `scripts/capture_imp_ui.py` renders
imp's own TUI from fixtures — the cheap, deterministic way to see the real
appearance without a live run (its human companion is `scripts/snap_imp_ui.py`).
This is the imp-specific home for that knowledge; the project-agnostic manual for
driving *any* terminal program is the `imp-driving-terminal-uis` skill. Its web
sibling — rendering and driving a page in a browser — is `imp-driving-web-uis`.
The README's example blocks and the `docs/examples/` transcripts (`seru99.md`,
`vida80.md`) are hand-maintained snapshots of real runs — the committed demo
journal (`docs/examples/seru99.jsonl.gz`) is a frozen artifact, no longer
replayed to regenerate them.

## Trailing output spacing

Human-facing subcommand output ends with exactly one trailing blank line when
stdout is a terminal (`sys.stdout.isatty()`) — never zero, never two; existing
trailing blanks collapse to one. Route list-building commands through
`_page_or_print(..., trailing_blank=True)` or `_emit_human(lines)`, both of which
reuse `_with_trailing_blank`. Piped/redirected output and machine formats
(`--json`, `log --html/--md/-`, `wrote <path>` confirmations) stay byte-clean —
no added blank line. New human subcommands must follow this rule.

## Rows during a live session

Between `LiveDriver` construction and `finish`, a row the session commits to the
terminal goes through `_emit_now` or `LiveDriver.commit_row`, never a direct
`print`. The driver tracks whether the last committed row is blank, and teardown
relies on that to leave exactly one blank row above the closing block.

## Error and diagnostic output

imp routes error and diagnostic surfaces through two shared sinks — no surface
styles its own line.

- **Quest-scoped terminal failures → the exit block.** A failure that identifies
  a quest (agent error, merge blocked, session-daemon startup failure, or a run
  that reported no outcome this client witnessed) renders as the gold three-part
  `_error_block` — red head + body + gold identity line — on **stdout**, via
  `_emit_merge_blocked` / `_emit_startup_failed` / `_emit_lost_connection` /
  `_emit_daemon_died`. Apart from the startup check, which fires before any
  stream exists, the closing block picks among them from what the client folded
  off the run's own event stream, never from a state it did not observe: an
  `end` frame carrying a leave disposition renders that leave's closing block
  (the one the client prints for its own leave), an `end` frame that carried no
  outcome is the daemon-died block, a stream cut before anything could be
  witnessed is the lost-connection block. The exit code carries the error signal
  for scripts; the codes are tabled in `guide/lifecycle.md` (*Exit codes*).
- **Non-quest CLI usage/lookup errors → one `imp:` message.** Bad arguments, no such
  quest, unknown command, and similar usage errors emit one
  `imp: <message>` on **stderr** via `_emit_cli_error`, which centralizes the
  prefix so no CLI call site hand-rolls it. The prefix lands on the first line only:
  a message may carry indented continuation lines (a `LoadError` hint, one auth hint
  per configured provider). Success confirmations carry no `imp:` prefix.

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

## Color in agent prose

Content-origin text is caret-escaped (`render_text._caret_escape`) so it cannot
alter terminal state. The one exception: SGR color in agent prose survives
verbatim inside a fenced code block and inside a single-line inline code span,
so a mockup keeps its colors and a short sample can sit inline. An agent cannot
emit the ESC byte, so a bare `[1;33m` in either place has the escape restored
first — never after a `^[`, which is a defused code being shown as itself.
Every other control byte, and any real escape outside a fence, still renders as
`^[`. Color-off output and the `--md`/`--html` exports strip SGR entirely, as does
the Telegram relay — with no renderer to re-arm anything, it strips *all* terminal
escapes at the outbound seam (`render_text.strip_terminal_escapes`), bare spellings
and cursor moves included.

Color-off output keeps inline code's source backticks, since color is otherwise its only marker.

## See also

- `internals/architecture.md` — the engine and renderer these surfaces are built on.
- `internals/invariants.md` — the layering rules that constrain renderer changes.
- `guide/introspection.md` — reading a quest's live state rather than styling it.
- `guide/lifecycle.md` — the exit-code contract scripts read.
- The full corpus index is the `imp-knowledge` skill's *More depth* section.

---

*Part of imp's self-knowledge corpus. Runtime entry point: load the `imp-knowledge` skill (`load_skill(name="imp-knowledge")`), the thin index that maps into this corpus.*
