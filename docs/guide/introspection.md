# imp introspection

How to answer a specific imp question about live state from ground truth instead
of memory: what the current workflow is doing, what work exists, and a quest's
event timeline. Each procedure says what to read or run and how to turn the raw
output into a plain-language answer. A spoke of `imp-knowledge` — load that hub
first for the stable picture, then come here to derive live specifics.

## What the current workflow is doing

Read the quest's `workflow/workflow.yaml` (under the quest home) for the ordered
phases and steps, then correlate it with the live step in `state/state.json` to
see where this quest is right now. Turn it into plain language: name the phase
the user is in and what the next step does, without surfacing internal step ids.

## Reading the workflow in depth

`workflow.yaml` is only the skeleton — the ordered phases and steps. The
actual instructions each step runs live beside it in the quest's
**workflow snapshot** at `.imp/quests/<id>/workflow/` — a copy of the live
workflow, refreshed at the start of each run, that is exactly what drove
this quest. When a question turns on what a step *does*, not just its name,
read the snapshot:

- `workflow.yaml` — phases, steps, tiers, vars: the order of what runs.
- `agents/<name>.md` — the full prompt a given agent step follows; where a
  step's real behavior lives.
- `partials/*.md` — shared prompt fragments the agent files include.
- `scripts/*.sh` — the shell scripts that `script` steps execute.
- `persona.md` — the workflow persona shared across agents.
- `skills/*.md` — the internal skills agents load (these docs among them).

Map a step name in `workflow.yaml` to its `agents/<name>.md` to read
exactly what that step was told to do.

## A CLI question

Run `imp help` for the command list, or `imp <cmd> --help` for one command's
flags and behavior. Read the help text back as the answer; quote the actual flag
names rather than guessing them.

## What work exists

- `imp list` — the board: active in-flight quests plus the backlog (including
  parked / `saved` quests).
- `imp list --all` — also includes completed and abandoned past quests.
- `imp search <query>` — a live-and-archive scout: search **active** (in-flight +
  drafts), **backlog**, and **archive** quests for `<query>` as a case-insensitive
  substring of a quest's id, title, or conversation content (a required substring;
  bare `imp search` errors). Composable `--active` / `--backlog` / `--archived` flags
  scope it to those zones; with none given, all three are searched. `--merged`
  narrows the archive to just the merged subset; add `--merged-after <commit>` to keep
  only merged quests whose merge landed after `<commit>` (implies `--merged`). Default
  output is grep-style matched excerpts per quest; `-l` / `--list`
  prints one terse line per quest (`id  title  [zone]`).
- `imp events search <query>` — the structured-event sibling of `imp search`: the
  same case-insensitive substring search, but over quests' **event content** (tool
  calls, tool results, agent reasoning, skill loads) rather than only their
  conversation prose — so it answers questions like which agents loaded a given
  skill, which `imp search` cannot. Searches **active**, **backlog** (matched on
  title + intent, since drafts have no events), and **archive** quests; the same
  `--active` / `--backlog` / `--archived` flags scope it, and `imp events`'
  `--type` / `--agent` filters narrow which events match (`--type tool_call` for
  "where invoked", the default for anywhere). Default output caps at the 3
  newest-matched quests with per-quest matched-event lines; `--limit` raises the
  cap and `-l` / `--list` prints one terse line per matched quest.

Summarize the rows in plain language (what is in flight, what is parked, what is
finished); do not invent quests that the commands did not list.

## Detail on one quest

`imp show <id>` — the overview detail card: id, title, status, timing,
dependencies, opening intent, and the summary inlined. A quest that completed with a
recorded merge SHA also shows a `merge_commit:` line beneath `base_branch:`. Add a
subcommand to narrow to one facet: `imp show <id> spec` for the raw spec, or add
`--json` to get the overview card as a structured object. For the event timeline,
use `imp events <id>` (below). Read the facet the user asked about and answer from
it.

## Reading an event timeline — three tiers

An event timeline has three distinct views. Reach for the lightest one that
answers the question, and never dump the raw log as your default.

- **Narrative** — `imp log <id>`. The human "read the story" view: prose with
  tool bursts folded. Use it when you want to follow what happened, in order.
  Scope it to a slice with the same windowing filters `imp events` accepts —
  `--step`, `--range` (or a positional `<range>`), `--limit`, and `--agent`
  (no `--type`, which fights the folded narrative) — and those filters carry
  through the `--md`/`--html` exports; `--follow` stays unfiltered. For example,
  `imp log <id> --step threshold` reads just the seed conversation.
- **Digest** — `imp events <id>`. A bare call prints a self-describing index
  (steps + types); add a leaf selector (`--type`, `--subtype`, `--wire`, `--agent`, a `<range>`, or
  `--all`) to get one compact line per event. On a TTY each compact line is
  clipped to one terminal row so the stream stays scannable; add `--full` to
  lift the clip and render the full untruncated line (still one logical line per
  event). Piped output is effectively unclipped already. This is the default for
  scanning and filtering an event stream from bash.
- **Raw** — `imp events <id> --json` (composes with any filter). The exact event
  payloads, including the full timestamp, uncapped. Drop to this tier only for
  the few events whose full contents you actually need.

## Reading another quest

To understand a *different* quest — live or archived — reach for the `imp` CLI
readers rather than cat-ing its files (a raw `read_file` silently fails for an
archived quest, whose artifacts live in a git ref, not on disk). The bash sandbox
mounts the whole host read-only, so any quest — current, other, or archived — is
readable:

- **`imp show <id>`** — the primary "what is this quest / what did it accomplish"
  read: the quest's detail card (id, title, status, timing, dependencies, the
  opening intent, and the summary inlined). Add `spec` for the raw spec, or
  `--json` for the card as a structured object.
- **`imp events <id>`** — the event history, when you need the trace rather than
  the distilled record. A bare call prints the index; steer into the high-signal
  types with `--type agent_result` (and away from the `tool_call` / `tool_result`
  / `agent_message` churn), or scope to an author with `--agent`. Resolves live and
  archived quests alike.

## Linking a related backlog quest

When you notice that an existing **backlog** quest relates to the work in front
of you — the same error, a shared root cause, or a fix here that supersedes it —
record that linkage on the *target* quest itself, not just in this quest's
ledger, with:

`imp edit <id> --append-intent "<note>"`

This appends the note to that quest's opening intent, so whoever picks it up
sees it. It works **only before that quest is picked up** (any quest not yet built or conversed with); once a quest is built or conversed with, the command is rejected.

Lead the note with your own quest id in brackets, then phrase the linkage as a
plain sentence that names the related quest — not a coined token like
`related-to`:

```
imp edit tepa94 --append-intent "[nuvu48] hit this same uploader timeout, likely the same root cause"
```

This lands the linkage on the referenced quest itself instead of only in the
deferring quest's ledger.

## Keep the output bounded

The event log grows without limit; pulling all of it floods your context. Scope
it deliberately:

- **Scope the query.** Narrow with `--limit` (the last N events), `--type` (a
  comma-separated event-type set), `--subtype` (message subtypes like
  `system_prompt`/`intent`), `--wire` (just the human + framework turns), and
  `--agent` (an author handle) rather than pulling the whole log.
- **Scan, then zoom.** Read the index or digest first; drop to `--json` only for
  the handful of events whose exact payload you need.
- **Prefer state over history.** When you only need where a quest stands now —
  not how it got there — reach for the board (`imp list`) or `imp show <id>`'s
  overview card instead of the event log.

## This quest's own state

Read `state/state.json` in the current quest home, or run `imp show <id>`
(the overview card), to report where this conversation's quest stands.

---

*Part of imp's self-knowledge corpus. Runtime entry point: load the `imp-knowledge` skill (`load_skill(name="imp-knowledge")`), the thin index that maps into this corpus.*
