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
**workflow snapshot** — a copy of the live workflow, refreshed at the start
of each run, that is exactly what drove this quest. It lives in the quest
home (`~/.imp/projects/<slug>/quests/<id>/workflow/`), which a quest's
worktree reaches through the symlink `.imp/quests/<id>/workflow/`. When a
question turns on what a step *does*, not just its name, read the snapshot:

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

- `imp list` — the board: active in-flight and draft quests, the backlog
  (parked items for later), and completed quests.
- `imp list --all` — also includes abandoned and archived quests; `--archived`
  alone shows only archived conversations.
- `imp list --json` — the same board as a structured JSON array, one
  object per quest tagged with its zone; composes with every filter.
  `name` is what the board shows — the title, else the opening intent or
  draft brief flattened to one line (`null` if none); `step` holds the
  board's display labels. For a quest's full intent, use
  `imp show <id> --json`.
- `imp search <query>` — a live-and-history scout: search **active** (in-flight +
  drafts), **backlog**, and **history** (completed, abandoned, archived) quests for
  `<query>` as a case-insensitive substring of a quest's id, title, or conversation
  content (a required substring; bare `imp search` errors). Composable `--active` /
  `--backlog` / `--completed` / `--abandoned` / `--archived` flags scope it, with
  `--archived` meaning archived conversations only; with none given, all three zones
  are searched. Add `--merged-after <commit>` to keep only completed quests whose
  merge landed after `<commit>` (implies `--completed`). Default output is grep-style matched
  excerpts per quest; `-l` / `--list` prints one terse line per quest
  (`id  title  status`). A miss prints a hint suggesting a next step.
- `imp events search <query>` — the structured-event sibling of `imp search`: the
  same case-insensitive substring search, but over quests' **event content** (tool
  calls, tool results, agent reasoning, skill loads) rather than only their
  conversation prose — so it answers questions like which agents loaded a given
  skill, which `imp search` cannot. Searches **active**, **backlog** (matched on
  title + intent, since drafts have no events), and **history** quests; the same
  `--active` / `--backlog` / `--completed` / `--abandoned` / `--archived` flags as
  `imp search` scope it, and `imp events`' `--type` / `--agent` filters narrow
  which events match (`--type tool_call` for "where invoked", the default for
  anywhere). Results stay zone-major — active →
  backlog → history — and newest-first within each zone. Each matching event shows
  as one digest row whose snippet is the 40 characters either side of the first
  match. Default rich and `--json`
  output show the first three matched quests and the first three matching events per
  quest; `--limit N` changes only the matched-quest bound. `--list` scans every
  matched quest unless capped with `--limit N`; `--all` lifts both search-result
  bounds for intentionally exhaustive rich or JSON output. Scan with `--list`, then
  retrieve exact payloads from a chosen quest with
  `imp events <id> <range> --json`. A miss prints a hint naming any scoping
  flags to drop.

Summarize the rows in plain language (what is in flight, what is parked, what is
finished); do not invent quests that the commands did not list.

## Detail on one quest

`imp show <id>` — the overview detail card: id, title, status, timing,
dependencies, opening intent, the summary inlined, the saved files (`files:`),
and the run ledger. A quest with recorded steps carries a `run:` line — work
time, tokens in and out, and the charge (its share of its week's quota, or the
dollar cost when no gauge readings exist for that week) — then a `workflow:`
line naming the workflow it ran under, and last on the card a `ledger:` section
holding the per-step table, minus steps that finished instantly with nothing to
report. A quest that has not run a step yet shows none of the three, and neither
does one still in the backlog.
A quest that completed with a recorded merge SHA also shows a `merge_commit:`
line beneath `base_branch:`; `--json` always carries `merge_commit` (`null`
when the quest has none). A quest halted as paused or error shows a
`reason:` line beneath `status:` naming why it stopped; `--json` carries the
raw token as `pause_reason`. A quest card shows a `vars:` line of the
variables you set with `--var`, and a backlog card the ones it was filed
with; either shows no line at all when there are none. Values are never
clipped: the line wraps between entries onto indented continuation lines at
the terminal width, and stays on one line when piped, so `imp show <id> |
grep vars:` returns it whole. A var whose value spans several lines or runs
past 80 characters leaves that line and gets its own `vars.<name>:` section
holding the value verbatim — after `summary:` on a quest card, after
`intent:` on a backlog card. A quest's `--json` carries its full effective
set under a `vars` key — workflow defaults, config overrides, and vars the
engine sets as it runs — and a backlog quest's carries its filed values.
A quest with saved files lists them in a `files:` section just before
`ledger:` — one path per line, alphabetical; `--json` carries them as `files`
(`[]` when none).
Name a saved file to print just that: `imp show <id> <name>` takes a path from
`files:` or a folder. A folder holding one file prints it —
`imp show <id> spec` for the raw spec, `imp show <id> verify` for the last
failing test-gate run — and a folder holding several lists their paths
(`imp show <id> plans`). It reads the live quest files while a quest is in
flight and the copy saved in git once it is finished. Or add `--json` to get
the overview card as a structured object. For the event timeline, use
`imp events <id>` (below). Read the facet the user asked about and answer from
it.

## Reading an event timeline — three tiers

An event timeline has three distinct views. Reach for the lightest one that
answers the question, and never dump the raw log as your default.

- **Narrative** — `imp log <id>`. The human "read the story" view: prose with
  tool bursts folded. Use it when you want to follow what happened, in order.
  Scope it to a slice with the same windowing filters `imp events` accepts —
  `--step`, `--range` (or a positional `<range>`), `--limit`, and `--agent`
  (no `--type`, which fights the folded narrative) — and those filters carry
  through the `--md`/`--html` exports, as do the content flags `--full` /
  `--show` / `--hide`: an export starts from the same lean view the terminal
  shows, `--show <cat>` adds a category as a collapsed `<details>` fold, and
  `--full` expands every fold inline. `--follow` takes no windowing filters
  but composes with the content flags. For example,
  `imp log <id> --step threshold` reads just the seed conversation. A
  subagent (or `-s` summary row) whose end falls outside the window still
  shows the state and duration it reached in the whole log.
- **Digest** — `imp events <id>`. A bare `imp events <id>` call prints a self-describing index
  (steps + types); add a leaf selector (`--type`, `--subtype`, `--wire`, `--agent`, a `<range>`,
  `--limit N`, or `--all`) to get one compact line per event. On a TTY each compact line is
  clipped to one terminal row so the stream stays scannable; add `--full` to
  lift the clip and render the full untruncated line (still one logical line per
  event). Piped output is effectively unclipped already. This is the default for
  scanning and filtering an event stream from bash.
- **Raw** — `imp events <id> --json` (composes with any filter). The exact event
  payloads, including the full timestamp, uncapped. Drop to this tier only for
  the few events whose full contents you actually need.

**Counting tool calls measures reach, not effort.** An agent that can shell out reads
files two ways, so a fall in `read_file` calls can conceal an equal rise in bash
`cat`/`sed`/`head`/`grep`. Count bash reads as reads, and prefer a measure of what was
touched — distinct files opened, overlap with the previous attempt — over call counts.

## The summary ledger

`imp log <id> -s` reads a run as a ledger rather than a story: one row per step,
each subagent it dispatched indented beneath it, carrying the step's working
time, its tokens in and out, and a right-most column that is either money or
quota. Working time runs only while a run is live: human waits, pauses, the
dormancy before a first run or a resume, and the gap after a crash are all
excluded, so attaching to, leaving, or renaming a quest never moves it. The
windowing filters above narrow the rows, and `--md` / `--html` export the same
table (`--summary` is the long form of `-s`).
The same table is on `imp show <id>` — a `ledger:` section at the foot of the
card, with its total row restated as a one-line `run:` field high up — except
that the card omits steps that finished instantly with nothing to report (under
a second, no model work, no subagents, no error); its total still counts them.
`imp log <id> -s` lists every step, and is where the windowing and export flags
live.

Rows and phase headings show the workflow's friendly labels (`finalizing spec`
under `spec phase`), and a step with no label shows its id. `--step` and
`imp events` take step ids, which a bare `imp events <id>` lists. A failed step
reads `· failed` after its name; a subagent that ended abnormally reads
`· interrupted`, `· stalled`, `· context exhausted`, `· suspended`, or `· failed`.

On a subscription run that right-most column is `weekly`: the share of its
week's quota this quest consumed, charged to *this* quest rather than read off
the gauge. For each week the run touched, imp takes the gauge's highest reading
that week and divides it across all imp work in the week — every quest's, in
every project — in proportion to the weighted work each did. A run is charged
its part of the week however long it paused and whatever ran beside it, and a
run that crosses a weekly reset is priced at each week's own rate, row by row.
The figure refines as the week goes on and is final once the week resets. It is
the figure the dollar column cannot give you — a subscription run spends no
money, and list prices are dominated by cache reads, which draw no quota on a
Claude subscription. Before the week's gauge first ticks off zero the total
reads `<1%`. A run on an API key keeps the `cost` column unchanged, and so does
a subscription run whose weeks have no gauge readings at all (history recorded
before imp read the gauge) — that case adds a dim legend line naming why.

Two reaches the figure does not cover:

- **Work imp cannot see.** The gauge counts every request on the account, but
  the week's work is found by walking `~/.imp/projects/`. A project whose state
  directory is hand-pinned with `$IMP_STATE_DIR` is not discoverable, so its
  usage — like any usage outside imp — is absorbed into this run's share and
  skews it high.
- **The gauge's own resolution.** The provider reports the weekly gauge in
  whole points, so the figure carries ±1 point of quantization — which is why a
  week whose gauge has not moved yet prices nothing and reads `<1%` rather than
  `0.00%`.

## Reading another quest

To understand a *different* quest — live or in history — reach for the `imp` CLI
readers rather than cat-ing its files (a raw `read_file` silently fails for an
archived quest, whose artifacts live in a git ref, not on disk). This project's
sibling quest homes are masked inside the bash sandbox and are not readable
there at all; these readers resolve them anyway, because the unsandboxed daemon
answers them — not the sandbox floor:

- **`imp show <id>`** — the primary "what is this quest / what did it accomplish"
  read: the quest's detail card (id, title, status, why it halted, timing, dependencies, the
  opening intent, the summary inlined, the saved files (`files:`), and the run
  ledger — a `run:` totals line and the per-step `ledger:` table minus idle
  steps). Add `spec` for the raw spec, `verify` for the last failing gate run,
  any other saved file or folder named in `files:` (`plans` prints the plan when
  there is only one, else lists them), or `--json` for the card as a structured
  object (which carries no ledger).
- **`imp events <id>`** — the event history, when you need the trace rather than
  the distilled record. A bare `imp events <id>` call prints the index; steer into the high-signal
  types with `--type agent_result` (and away from the `tool_call` / `tool_result`
  / `agent_message` churn), or scope to an author with `--agent`. Resolves live and
  history quests alike.

Other projects' state directories *are* readable read-only inside the sandbox,
so their `quests/<id>/events.jsonl` and artifacts can be read directly — a
fallback for cross-project diagnosis only. **This** project's history is
unreachable that way (its sibling quest homes and archive are masked); search it
with `imp search` / `imp events search`, which the daemon resolves. The
`imp` CLI reaches another project only with both env pins cleared —
`cd <other repo> && IMP_SESSION_SOCKET= IMP_STATE_DIR= imp log <id>` — since
the socket pins reads to this quest's daemon and `IMP_STATE_DIR` to this
project. Running `imp` from another project's directory without clearing both
pins is refused.

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
