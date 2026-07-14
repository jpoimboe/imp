# Quest lifecycle

A reference for how a quest moves from idea to merge: its statuses, where it
appears in the UI, and what happens when you leave a conversation mid-stream.

## Quest and its home

A **quest** is one unit of work imp tracks from idea to merge. Each quest has a
per-quest home on disk at `.imp/quests/<id>/` holding its spec, plans, events,
and state — its whole record lives there. The home also holds the quest's
**workflow snapshot** at `workflow/` — the `workflow.yaml` plus the agent
prompts, partials, scripts, and skills that drove it.

## Statuses

A quest carries one at-rest status: `new`, `paused`, `saved`, `completed`,
`merge_blocked`, `abandoned`. `running` is **derived** — a live process holds
the quest's lock — and is never stored. A `new` or `paused` quest that has not
been built yet (un-materialized) is shown to the user as a **draft**.

## Zones: active list, backlog, archive

Where a quest appears is **derived** — never a separate status string. Two facts
decide it: whether it has been **materialized**, and its **origin**.

- **active list** — quests in flight, plus un-materialized **drafts**: a fresh
  `imp new` draft has `origin: draft`, so it shows here relabeled `draft`, not
  in the backlog.
- **backlog** — un-materialized backlog items (`origin: backlog`) parked for
  later; pick one up when you are ready.
- **archive** — finished (`completed`/`abandoned`) and shelved conversations,
  reachable by search.

## Materialize / go-live

A draft has **no branch and no worktree** until the user builds it. Going live
calls `create_and_enter_worktree`, which forks a fresh branch and worktree from
the base branch's current tip and switches the quest into it. Before that, a
draft is just a conversation and its artifacts on disk.

## Entry points

- `imp new` — start a fresh quest; a left draft stays in the active list, shown
  as a `draft` (not the backlog), resumable later.
- `imp chat` — open the threshold conversation.
- `imp resume <id>` — pick a parked or left quest back up.

## What happens to a left draft

When a phase-0 conversation is left, its fate depends on what happened:

- **Never answered / no real exchange** → `discard_conversation` (it goes away).
- **`imp new`, an explicit `/keep`, or the quest was renamed (a design formed)**
  → `save_as_draft`: `origin` stays `draft`, so it remains in the active list,
  shown as a **draft**, resumable later. It does **not** move to the backlog.
- **Otherwise** (an un-renamed `imp chat` conversation) → `archive_conversation`:
  it leaves the active list but stays resumable (shelved) via `imp resume` or
  findable with `imp search`.

This is the exact point **training data** gets wrong — it once claimed `imp new`
"archives a draft as resumable"; it does not. `imp new` runs `save_as_draft`, so
the draft stays in your active list, shown as a `draft`, rather than being
archived. Likewise, a renamed `imp chat` conversation is kept as an active draft,
not archived — the rename (a design formed), not the entry point, is what keeps
it in the active list; and `save_as_draft` never moves anything to the backlog.

---

*Part of imp's self-knowledge corpus. Runtime entry point: load the `imp-knowledge` skill (`load_skill(name="imp-knowledge")`), the thin index that maps into this corpus.*
