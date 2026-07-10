# imp introspection

How to answer a specific imp question from ground truth instead of memory. Each
procedure says what to read or run and how to turn the raw output into a
plain-language answer. Pair with `imp-mental-model` for the stable picture.

## What the current workflow is doing

Read the change's `workflow/workflow.yaml` (under the change home) for the ordered
phases and steps, then correlate it with the live step in `state/state.json` to
see where this change is right now. Turn it into plain language: name the phase
the user is in and what the next step does, without surfacing internal step ids.

## A CLI question

Run `imp help` for the command list, or `imp <cmd> --help` for one command's
flags and behavior. Read the help text back as the answer; quote the actual flag
names rather than guessing them.

## What work exists

- `imp show` — the board: active in-flight changes plus the Backlog (including
  parked / `saved` changes).
- `imp show --all` — also includes completed and abandoned past changes.
- `imp search <query>` — a live-and-archive scout: search **active** (in-flight +
  drafts), **backlog**, and **archive** changes for `<query>` as a case-insensitive
  substring of a change's id, title, or conversation content (a required substring;
  bare `imp search` errors). Composable `--active` / `--backlog` / `--archived` flags
  scope it to those zones; with none given, all three are searched. Default output is
  grep-style matched excerpts per change; `-l` / `--list` prints one terse line per
  change (`id  title  [zone]`).

Summarize the rows in plain language (what is in flight, what is parked, what is
finished); do not invent changes that the commands did not list.

## Detail on one change

`imp show <id> [spec|plan|events|status]` — the subcommand selects the
facet: `spec` for the spec, `plan` for the plan(s), `events` for the timeline,
`status` for where it stands. Read the facet the
user asked about and answer from it.

## Reading an event timeline — three tiers

An event timeline has three distinct views. Reach for the lightest one that
answers the question, and never dump the raw log as your default.

- **Narrative** — `imp log <id>`. The human "read the story" view: prose with
  tool bursts folded. Use it when you want to follow what happened, in order.
- **Digest** — `query_events(...)` in-loop, or `imp show <id> events` from bash.
  One compact line per event. This is the default view for scanning and
  filtering, and it is what both front doors return unless you ask for raw.
- **Raw** — `full=True` (the tool) or `--full` on `imp show <id> events` (the
  CLI). The exact event
  payloads, with `step_context` redacted and each event byte-capped. Drop to
  this tier only for the few events whose full contents you actually need.

## Querying another change's timeline

To read a *different* change's events, do not cat its log file — use a front
door that renders a digest by default:

- In-loop: `query_events(change_id="<id>")` returns that change's digest (or its
  raw payloads with `full=True`). It resolves live and archived changes alike,
  and returns a clear `{"error": ...}` string for an unknown id instead of
  raising.
- From bash: `imp show <id> events` prints that change's digest lines (add
  `--full` for the raw payloads).

## Keep the output bounded

The event log grows without limit; pulling all of it floods your context. Scope
it deliberately:

- **Scope the query.** Narrow with `--limit` / `limit` (the last N events),
  `--type` / `type` (a comma-separated event-type set), and `--agent` /
  `agent_id` (an agent-id substring) rather than pulling the whole log.
- **Scan, then zoom.** Read the digest first; drop to `--full` / `full=True`
  only for the handful of events whose exact payload you need.
- **Prefer state over history.** When you only need where a change stands now —
  not how it got there — reach for the board (`imp show`) or the `status` /
  `spec` facets of `imp show <id>` instead of the event log.

## This change's own state

Read `state/state.json` in the current change home, or run
`imp show <id> status`, to report where this conversation's change stands.
