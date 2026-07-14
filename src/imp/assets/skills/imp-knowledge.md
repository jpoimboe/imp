# imp knowledge

The entry point to imp's self-knowledge: a stable primer plus a **map into imp's
docs corpus**. Load this first whenever you need to ground an answer about how imp
works, then follow the map to the corpus doc the question needs. Everything
specific (the live workflow, what quests exist, what memory is active) is read
fresh — this file is only the durable index.

## The corpus (the map)

imp's *descriptive* self-knowledge lives in a docs corpus resolved on disk at the
path injected into every agent prompt as `{{ docs.path }}`. The corpus is resolved
**in place** — never copied into a quest snapshot — so it is readable in **any**
project. Reach it with `bash` and `read_file` using absolute paths under
`{{ docs.path }}`; the `grep` and `glob` *tools* are worktree-bound and will not
see it. For example:

```bash
grep -rn "materialize" {{ docs.path }}/guide
```

Two areas live under that path:

- **`guide/`** — imp's descriptive self-knowledge (the portable expert corpus):
  `what-is-imp.md` (conceptual intro), `features.md` (capabilities catalog),
  `lifecycle.md` (quest statuses, the derived zones, materialize / go-live, entry
  points, and what happens to a left draft), `introspection.md` (reading live
  state via the `imp` CLI), `memory.md` (what imp remembers and where),
  `operator-notes.md` (runbook for a live quest).
- **`internals/`** — imp-on-imp contributor reference: `architecture.md`,
  `conventions.md`, `invariants.md`, `subagent-authoring.md`, and
  `knowledge-map.md` (how this self-knowledge is organized and where new knowledge
  belongs).

## Core vocabulary

- **spec** — what the quest must accomplish.
- **plan** — the step-by-step implementation a coder executes.
- **phase** — a stage of the workflow (threshold, spec, plan, implement,
  integrate).
- **step** — one agent or gate run inside a phase.
- **tier** — the model capability level a step runs at, chosen per role from
  cheapest to strongest.
- **workflow** — the ordered phases/steps imp drives a quest through, defined in
  the quest's `workflow/workflow.yaml`.
