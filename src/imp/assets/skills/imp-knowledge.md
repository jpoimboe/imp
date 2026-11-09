# imp knowledge

## What imp is

imp drives a software **quest** — one unit of work — from idea to merged code
through a structured, reviewable sequence of phases. A fresh quest starts as a
**draft** (a conversation and notes on disk, no git branch) until you choose to
build it.

## What imp can do

No more babysitting Claude Code. With imp you work at the level of intent: you say
what you want, and it owns the mechanism of turning that into a reviewed, tested,
and integrated change. Each step runs with fresh, tightly-scoped context instead
of one sprawling chat session, so there's nothing to hover over and the code that
lands stays maintainable.

- **Intent-driven.** State intent in the opening threshold interview — the primary
  human touchpoint — then imp drives the rest end-to-end (build, reviews, and
  merge) on its own, surfacing again only if something needs discussing at merge
  (e.g. a follow-up to file).
- **Draft-first scoping.** A fresh quest is a draft — a conversation with zero
  commitment — that you later build or park.
- **Adversarial review.** Multiple automated rounds of design review, plan
  review, code review, and testing — reviewers kept independent by model tier —
  plus automatic merge-conflict resolution. The spec and the plan are each
  reviewed by a fresh agent that did not write them, before any code exists.
- **Cleaner code by design.** imp's coding stance pushes back on the reflexive
  fallbacks and dead error-handling agents pile up, so changes stay lean and
  maintainable.
- **Isolated & sandboxed.** Each change lives in its own git worktree; agent
  shell/tool access is sandboxed.
- **Lean context.** Every step runs as a fresh agent with tightly-scoped context,
  so there's no long session to drift — the root of most agent failures.
- **Deterministic engine.** The engine decides nothing about your code — it
  sequences steps, routes on their results, and records every byte to an
  append-only log that is the single source of truth.
- **Pause, resume, interrupt.** Ctrl-D or `/pause` to set a quest aside for later;
  Ctrl-C to interrupt a running agent and re-steer; a run interrupted mid-call
  resumes from the log with nothing lost.
- **Full provenance.** Trace any line of code back through the run to the
  conversation that spawned it.
- **Search & backlog.** Full search across every quest, and a tracked backlog
  agents can file ideas into for you.
- **Project memory.** Standing instructions applied to every run.
- **Remote & parallel.** Steer a run from chat (Telegram), and run several imps at
  once, one per terminal tab.
- **Provider-flexible.** Claude and Codex CLI subscriptions plus OpenAI and
  Anthropic APIs, per-role and multi-vendor in one run; imp uses its own tools (not
  the vendor CLIs) for consistent cross-provider behavior (production-tested with
  Claude Code so far). Setup (`imp init`) detects the default workflow, integration
  branch, fast test command, `worktree_setup`, and Python import path and confirms
  them on one screen, needing no provider; `--use-defaults` (with optional
  `--workflow`/`--base`/`--pythonpath`) runs it non-interactively, no-TTY
  invocations take that path automatically, and `--providers a,b` skips the
  picker in either mode. One quest can override that order: `--provider NAME` on
  `imp new` / `imp chat` / `imp resume` pins it to one vendor, and `/provider`
  at a prompt moves a running quest to another at the next step boundary.
- **Batteries-included, customizable.** A default workflow encoding current
  best-practice agentic engineering; author your own if you like.
- **Beyond software.** Not limited to code — imp can drive other structured
  processes (e.g. business workflows).

## Introspect with the `imp` CLI

Don't dig through `.imp/` — the raw event log is masked and the rest is internal
bookkeeping. Ask the CLI instead:

- `imp list` — the board: in-flight quests and drafts, the backlog, and
  completed quests; `--all` adds abandoned and archived quests, `--json` emits
  the board as a structured array.
- `imp show <id>` — a quest's card: status, why it halted or its pending
  question, current step, `--var` values (`vars:`), intent, summary, the saved
  files (`files:`), and the run ledger — a `run:` line of time, tokens, and
  charge (the run's share of its week's quota, or its cost when no gauge
  readings exist for that week), `workflow:`, and the per-step `ledger:` table
  minus idle steps (`--json` for a structured card with every effective var, no
  ledger). `show <id> <name>`
  prints one saved file: a `files:` path, or a folder — a one-file folder prints
  its file (`show <id> spec`, `show <id> verify` for the last failing gate run),
  a several-file folder lists them (`show <id> plans`).
- `imp search <query>` — case-insensitive substring search across in-flight,
  backlog, and history quests' conversation content.
- `imp log <id>` — the human "read the story" narrative, tool bursts folded;
  `--step <name>` scopes it to one step, `-s` swaps the story for the full
  per-step ledger, idle steps included (rows show labels, `--step` takes ids),
  and owns its windowing and `--md`/`--html` export.
- `imp events <id>` — the agent-facing digest, where event content actually
  lives: bare prints an index (steps, subagents, types); add a leaf selector
  (`--limit N`, `--type`, `--agent <handle>`, a range, `--all`) for one compact
  line per event (rows cap at 200 chars, `--full` lifts), `--step <name>` to
  scope, `--json` for exact payloads.
- `imp events search <query>` — structured-event search is bounded by default;
  `--type` / `--agent` narrow it. Use `--list` to scan every matched quest,
  `imp events <id> ... --json` for exact payloads, and `--all` only for exhaustive
  search output.
- `imp edit <id>` — link or annotate a quest that hasn't run yet: `--append-intent
  "[<quest>] <note>"` cross-links a note onto another quest's opening intent (only
  while that quest is still an untouched draft — rejected once it's been built or even
  conversed with); `--depends-on <id>` adds a dependency edge (only before go-live —
  rejected once materialized). `--base <branch>` re-points the fork at another existing
  branch (also before go-live only).
- `imp config` — the effective settings and the layer each came from, led by this
  checkout's `project state:` directory (not under `--explain`), suffixed
  `(not created yet)` before the first claim;
  `imp config set <key> <value>` / `unset <key>` edits the setting's own layer
  (the main checkout's `imp/config.yaml` or `~/.imp/config.yaml`; `--project` /
  `--global` / `--local` forces one — `--local` is this checkout's uncommitted
  `~/.imp/projects/<name>/config.yaml`, top of the stack), preserving comments.
- `imp help`, `imp <cmd> --help` — the command list, or one command's flags.

The event log is unbounded — filter (`--step`, `--agent`, `--limit`) rather than
dumping it all into your context.

## Memory

Standing, cross-quest instructions live in the repo's `imp/memory/` directory,
layered broad→narrow (`all.md` → every agent; `<workflow>/<step>.md` → one step).
Keep each memory very succinct and use it sparingly — every line is injected into
agent context on every run; for anything long, keep a doc and point the memory at
its path. Don't hand-edit the tree. A standalone change is recorded with a background
memory quest (`imp new --workflow memory --background --autonomous "<what to change>"`); a change
coupled to a code quest rides that quest's spec as a Memory changes item and lands
with the code.

## More depth

imp's full self-knowledge corpus is at `{{ docs.path }}`, read in place from any
project. Go straight to the file whose line below matches the question — don't
list the directory.

Using imp — relevant in any project:

- `guide/what-is-imp.md` — the quest model and the six phases, end to end.
- `guide/lifecycle.md` — statuses, zones, drafts, what a left conversation becomes, and the exit-code table.
- `guide/features.md` — the capability catalog, one section per capability.
- `guide/introspection.md` — answering a live-state question from ground truth: the workflow snapshot, event timelines, the summary ledger, keeping output bounded.
- `guide/memory.md` — what imp remembers, where it lives, how to inspect it.
- `guide/operator-notes.md` — runbook for a running quest: unblocking a pause, recovering a dead provider login, the undelivered-message warning, snapshot and archive version skew.
- `guide/remote-control.md` — driving a run from a chat surface.
- `guide/embedding.md` — the `imp.api` surface for driving quests from another process.

Working on imp's own source — skip these unless the project under change *is* imp:

- `internals/architecture.md` — engine and executor step loop, the event log, where state lives.
- `internals/conventions.md` — terminal output, user-facing copy, code comments.
- `internals/invariants.md` — what must hold when editing the engine, renderer, or workflow prompts.
- `internals/knowledge-map.md` — which layer new knowledge belongs in: skill, corpus, or workflow prompt.

## Glossary

- **spec** — what the quest must accomplish
- **plan** — a coding/testing plan
- **phase** — a stage of the workflow
- **step** — one agent or script run
- **tier** — the model capability level a step runs at
- **workflow** — the ordered phases and steps
