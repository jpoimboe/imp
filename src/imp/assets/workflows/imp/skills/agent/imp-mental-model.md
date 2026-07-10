# imp mental model

The stable primer for how imp is organized. Load this when you need the shape of
imp itself; load `imp-introspect` to derive a specific answer from live state.
Everything specific (the live workflow, what changes exist) is read fresh — this
file is only the durable picture.

## Change and its home

A **Change** is one unit of work imp tracks from idea to merge. Each Change has a
per-change home on disk at `.imp/quests/<id>/` holding its spec, plans, events,
and state — its whole record lives there.

## Statuses

A Change carries one at-rest status: `new`, `paused`, `saved`, `completed`,
`merge_blocked`, `abandoned`. `running` is **derived** — a live process holds the
change's lock — and is never stored. A **new** or **paused** Change that has not
been built yet (un-materialized) is shown to the user as **draft**.

## Zones: active list, Backlog, archive

Where a Change appears is **derived** — never a separate status string. Two facts
decide it: whether it has been **materialized**, and its **origin**.

- **Active list** — Changes in flight, plus un-materialized **drafts**: a fresh
  `imp new` draft has `origin: draft`, so it shows here relabeled `draft`, not in
  the Backlog.
- **Backlog** — un-materialized **ideas / follow-ups** (`origin` of `idea` or
  `followup`) parked for later; pick one up when you are ready.
- **Archive** — finished (`completed`/`abandoned`) and left conversations,
  reachable by search.

## Materialize / go-live

A draft has **no branch and no worktree** until the user builds it. Going live
calls `create_and_enter_worktree`, which forks a fresh branch and worktree from
the base branch's current tip and switches the Change into it. Before that, a
draft is just a conversation and its artifacts on disk.

## Entry points and what happens to a left draft

- `imp new` — start a fresh change; a left draft stays in the active list, shown
  as a `draft` (not the Backlog), resumable later.
- `imp chat` — open the threshold conversation.
- `imp resume <id>` — pick a parked or left Change back up.

When a phase-0 conversation is left, its fate depends on what happened:

- **Never answered / no real exchange** → `discard_conversation` (it goes away).
- **`imp new`, or an explicit `/save`** → `save_to_backlog`: it stays in the
  active list, shown as a **draft**, resumable later.
- **Otherwise** → `archive_conversation`: it leaves the active list but stays
  resumable via `imp resume` or findable with `imp search`.

This is the exact point training data gets wrong (it once claimed `imp new`
"archives a draft as resumable" — it does not; `imp new` runs `save_to_backlog`,
so the draft stays in your active list, shown as a `draft`, rather than being
archived).

## Core vocabulary

- **spec** — what the change must accomplish.
- **plan** — the step-by-step implementation a coder executes.
- **phase** — a stage of the workflow (threshold, spec, plan, implement,
  integrate).
- **step** — one agent or gate run inside a phase.
- **lane** — a model tier a step runs at.
- **workflow** — the ordered phases/steps imp drives a change through, defined in
  the change's `workflow/workflow.yaml`.
