# imp knowledge

The entry point to imp's self-knowledge: a stable primer on how imp is organized,
plus a map to imp's deeper skills. Load this first whenever you need to ground an
answer about how imp works, then follow the map to the skill the question needs.
Everything specific (the live workflow, what quests exist, what memory is
active) is read fresh — this file is only the durable picture.

## Deeper skills (the map)

Load these with `load_skill(name=...)` when a question goes past the primer below:

- **`imp-introspection`** (`load_skill(name="imp-introspection")`) — how to read
  live state from ground truth: what the current workflow is doing, what work
  exists, and a quest's event timeline. Load it when a question turns on the state
  of a specific quest right now.
- **`imp-memory`** (`load_skill(name="imp-memory")`) — what imp remembers and
  where: its two tiers of durable state (per-quest records and project-level
  `imp/project.yaml` config and `imp/memory/` files), the layered memory model
  in depth, and how imp can help you author memory in the interactive gates, not
  just inspect it. Load it for any question about memory, persistence, or project
  configuration.

## Quest and its home

A **quest** is one unit of work imp tracks from idea to merge. Each quest has a
per-quest home on disk at `.imp/quests/<id>/` holding its spec, plans, events,
and state — its whole record lives there. The home also holds the quest's
**workflow snapshot** at `workflow/` — the `workflow.yaml` plus the agent
prompts, partials, scripts, and skills that drove it; `imp-introspection` covers
reading it in depth.

## Statuses

A quest carries one at-rest status: `new`, `paused`, `saved`, `completed`,
`merge_blocked`, `abandoned`. `running` is **derived** — a live process holds the
quest's lock — and is never stored. A **new** or **paused** quest that has not
been built yet (un-materialized) is shown to the user as **draft**.

## Zones: active list, backlog, archive

Where a quest appears is **derived** — never a separate status string. Two facts
decide it: whether it has been **materialized**, and its **origin**.

- **active list** — quests in flight, plus un-materialized **drafts**: a fresh
  `imp new` draft has `origin: draft`, so it shows here relabeled `draft`, not in
  the backlog.
- **backlog** — un-materialized **backlog items** (`origin` `backlog`) parked for
  later; pick one up when you are ready.
- **archive** — finished (`completed`/`abandoned`) and shelved conversations,
  reachable by search.

## Materialize / go-live

A draft has **no branch and no worktree** until the user builds it. Going live
calls `create_and_enter_worktree`, which forks a fresh branch and worktree from
the base branch's current tip and switches the quest into it. Before that, a
draft is just a conversation and its artifacts on disk.

## Entry points and what happens to a left draft

- `imp new` — start a fresh quest; a left draft stays in the active list, shown
  as a `draft` (not the backlog), resumable later.
- `imp chat` — open the threshold conversation.
- `imp resume <id>` — pick a parked or left quest back up.

When a phase-0 conversation is left, its fate depends on what happened:

- **Never answered / no real exchange** → `discard_conversation` (it goes away).
- **`imp new`, or an explicit `/keep`** → `save_to_backlog`: it stays in the
  active list, shown as a **draft**, resumable later.
- **Otherwise** → `archive_conversation`: it leaves the active list but stays
  resumable (shelved) via `imp resume` or findable with `imp search`.

This is the exact point training data gets wrong (it once claimed `imp new`
"archives a draft as resumable" — it does not; `imp new` runs `save_to_backlog`,
so the draft stays in your active list, shown as a `draft`, rather than being
archived).

## Core vocabulary

- **spec** — what the quest must accomplish.
- **plan** — the step-by-step implementation a coder executes.
- **phase** — a stage of the workflow (threshold, spec, plan, implement,
  integrate).
- **step** — one agent or gate run inside a phase.
- **lane** — a model tier a step runs at.
- **workflow** — the ordered phases/steps imp drives a quest through, defined in
  the quest's `workflow/workflow.yaml`.
