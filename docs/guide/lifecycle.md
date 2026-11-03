# Quest lifecycle

A reference for how a quest moves from idea to merge: its statuses, where it
appears in the UI, and what happens when you leave a conversation mid-stream.

## Quest and its home

A **quest** is one unit of work imp tracks from idea to merge. Each quest has a
per-quest home on disk at `~/.imp/projects/<slug>/quests/<id>/`, outside the
checkout, holding its spec, plans, events, and state — its whole record lives
there. From inside the quest's own worktree that same home is reachable as
`.imp/quests/<id>/`, a symlink. The home also holds the quest's
**workflow snapshot** at `workflow/` — the `workflow.yaml` plus the agent
prompts, partials, scripts, and skills that drove it.

## Statuses

A quest carries one at-rest status: `new`, `paused`, `merge_blocked`, `merged`,
`archived`, `abandoned`, `error`. The word `imp list` shows is **derived** from that
at-rest status plus two runtime facts — whether a live process holds the quest's
lock, and a durable **halt-condition** recorded on `state.json`:

- `running` — a live process holds the lock and is working.
- `waiting` — a live process is parked at a human input gate (halt-condition
  `waiting`).
- `error` — a not-live failure halt (stored `error`).
- `merged` — merged into its base branch (stored `merged`).
- `archived` — an archived, resumable conversation (stored `archived`).
- `merge blocked` — the merge gate needs a decision (stored `merge_blocked`).
- `paused` — not live, a clean stop (a yield or interrupt).
- `new` — created, never started.

Only `running` and `waiting` are derived and never stored; every other word is
stored verbatim and shown as-is (only `merge_blocked` renders with a space).
There is no `draft` status word: an
un-materialized quest shows its real derived status (`new`/`paused`), with its
`(threshold)` step marking that it is still being scoped.

## Zones: active list, backlog, archive

Where a quest appears is **derived** — never a separate status string. Two facts
decide it: whether it has been **materialized**, and its **origin**.

- **active list** — quests in flight, plus un-materialized **drafts**: a fresh
  `imp new` draft has `origin: draft`, so it appears in the active list, not the
  backlog.
- **backlog** — un-materialized backlog items (`origin: backlog`) parked for
  later; pick one up when you are ready.
- **archive** — finished (`merged`/`abandoned`) and archived conversations,
  reachable by search.

## Materialize / go-live

A draft has **no branch and no worktree** until the user builds it. Going live
calls `create_and_enter_worktree`, which forks a fresh branch and worktree from
the base branch's current tip and switches the quest into it. Before that, a
draft is just a conversation and its artifacts on disk. If the project sets
`worktree_setup`, imp runs that command in the new worktree before the first
step — and re-runs it on every `create_and_enter_worktree` call, since a fresh
worktree has none of the gitignored per-tree directories a toolchain needs. A
failure pauses the quest with the command's output.

## Entry points

- `imp new` — start a fresh quest; a left draft stays in the active list (not the
  backlog), resumable later.
- `imp chat` — open the threshold conversation.
- `imp resume <id>` — pick a parked or left quest back up; resuming one whose
  session is still live attaches to (joins) that live run.

## What happens to a left draft

When a phase-0 conversation is left, its fate depends on what happened:

- **Never answered / no real exchange** (and no `/pause`) → `discard_conversation`
  (it goes away).
- **`imp new`, an explicit `/pause`, or the quest was renamed (a design formed)**
  → `save_as_draft`: the quest is `paused` and `origin` stays `draft`, so it
  remains in the active list, resumable later. It does **not** move to the backlog.
- **After a real exchange, an explicit `/archive` or `/clear`, or otherwise** (an
  un-renamed `imp chat` conversation) → `archive_conversation`: it leaves the
  active list but stays resumable (`archived`) via `imp resume` or findable with
  `imp search`. `/clear` then opens a fresh conversation.

On a built quest `/archive` and `/clear` are refused; `/pause` pauses it exactly
like Ctrl-D at that prompt.

This is the exact point **training data** gets wrong — it once claimed `imp new`
"archives a draft as resumable"; it does not. `imp new` runs `save_as_draft`, so
the draft stays in your active list, rather than being archived. Likewise, a renamed
`imp chat` conversation is kept as an active draft,
not archived — the rename (a design formed), not the entry point, is what keeps
it in the active list; and `save_as_draft` never moves anything to the backlog.

## Exit codes

A script can tell from imp's exit code alone how a run ended:

| Code | Meaning |
|---|---|
| 0 | Finished: merged or done, or the command succeeded |
| 1 | The run stopped on a failure (error pause, merge blocked, crash) |
| 2 | Usage error: bad arguments, no such quest |
| 3 | The session failed to start |
| 4 | Lost the connection to the run (it may still be running) |
| 130 | You ended it and nothing failed: left, paused, archived, discarded, abandoned or cancelled. A left quest is resumable with `imp resume`. |

---

*Part of imp's self-knowledge corpus. Runtime entry point: load the `imp-knowledge` skill (`load_skill(name="imp-knowledge")`), the thin index that maps into this corpus.*
