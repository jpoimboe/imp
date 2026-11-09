# What is imp?

imp is a tool for taking a software quest from idea all the way to merge,
driving it through a structured workflow so the work stays organized at every
stage.

The core idea is a **quest** — one unit of work imp tracks from initial idea to
merged code. Each quest moves through a sequence of phases:

- **threshold** — conversation: answering questions, taking stock, or scoping a
  new quest.
- **spec** — pinning down what the quest must accomplish.
- **plan** — turning that into a step-by-step implementation.
- **implement** — a coder executing the plan.
- **converge** — reviewing everything built, once.
- **integrate** — testing and landing the result.

A few things worth knowing:

- A fresh quest starts as a **draft** — just a conversation and some notes on
  disk, with no git branch or worktree until you decide to actually build it. So
  you can scope something out with zero commitment, then either build it now or
  save it for later.
- imp keeps your work in **zones**: an active list of things in flight, a
  **backlog** of parked ideas, and a searchable **history** of finished,
  abandoned, or archived work.
- Everything for a quest — its spec, plans, and history — lives together in one
  per-quest home on disk.

In practice, the threshold phase can answer how imp works, report on what work you
already have in flight, or help you scope and kick off something new.

---

*Part of imp's self-knowledge corpus. Runtime entry point: load the `imp-knowledge` skill (`load_skill(name="imp-knowledge")`), the thin index that maps into this corpus.*
