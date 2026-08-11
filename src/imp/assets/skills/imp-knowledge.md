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
- **Pause, resume, interrupt.** Ctrl-D to set a quest aside for later; Ctrl-C to
  interrupt a running agent and re-steer; a run interrupted mid-call resumes from
  the log with nothing lost.
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
  branch, fast test command, and import root and confirms them on one screen, needing
  no provider; `--use-defaults` (with optional `--workflow`/`--base`/`--pythonpath`)
  runs it non-interactively, and no-TTY invocations take that path automatically.
- **Batteries-included, customizable.** A default workflow encoding current
  best-practice agentic engineering; author your own if you like.
- **Beyond software.** Not limited to code — imp can drive other structured
  processes (e.g. business workflows).

## Introspect with the `imp` CLI

Don't dig through `.imp/` — the raw event log is masked and the rest is internal
bookkeeping. Ask the CLI instead:

- `imp list` — the board: in-flight quests and drafts, the backlog, and
  completed quests (merged or done); `--all` adds abandoned and shelved
  conversations.
- `imp show <id>` — a quest's card: status, current step, intent, and summary
  (`show <id> spec` for the raw spec, `--json` for a structured card).
- `imp search <query>` — case-insensitive substring search across in-flight,
  backlog, and archived quests' conversation content.
- `imp log <id>` — the readable narrative of what happened; `--step <name>` scopes
  it to one step.
- `imp events <id>` — the event index (steps, subagents, types); add `--step
  <name>` for a per-agent breakdown, or `--agent <handle>` to follow one subagent.
- `imp events search <query>` — the same substring search over structured event
  content (tool calls, results, reasoning) across all quests, reaching what `imp
  search` (prose only) can't; `--type` / `--agent` narrow it.
- `imp edit <id>` — link or annotate a quest that hasn't run yet: `--append-intent
  "[<quest>] <note>"` cross-links a note onto another quest's opening intent (only
  while that quest is still an untouched draft — rejected once it's been built or even
  conversed with); `--depends-on <id>` adds a dependency edge (only before go-live —
  rejected once materialized). `--base <branch>` re-points the fork at another existing
  branch (also before go-live only).
- `imp config` — the effective settings and the layer each came from, led by this
  checkout's `project state:` directory (not under `--explain`);
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
memory quest (`imp new --workflow memory --background "<what to change>"`); a change
coupled to a code quest rides that quest's spec as a Memory changes item and lands
with the code.

## More depth

imp's full self-knowledge lives at `{{ docs.path }}`.

## Glossary

- **spec** — what the quest must accomplish
- **plan** — a coding/testing plan
- **phase** — a stage of the workflow
- **step** — one agent or script run
- **tier** — the model capability level a step runs at
- **workflow** — the ordered phases and steps
