---
name: front-door
description: An open conversation that scopes a small change and, when the user is ready, goes live by forking a worktree on imp/<id> and building it there — no plan, no merge.
subagents: [explorer, researcher, spiker]
drivers: [impty]
output:
  status: {type: enum, values: [Success, Error]}
---

{% include "partials/front-door.md" %}

## Going live (forking a worktree)

This is the **light** workflow: no plan is written and no merge happens. Going live forks
an isolated worktree on `imp/{{ quest.id }}`; the change is built and committed there,
leaving the commits on that branch for the user to integrate manually. When the user
wants to build, do these in one turn:

1. **Name the quest if it is still unnamed.** Call `rename_quest(name="…")` once the
   intent is clear and announce the name in prose without asking permission (titled as
   under "Naming the quest early").
2. **Write the final spec.** Write the complete, build-ready specification — Goal, and
   Scope / Decisions / Acceptance criteria as far as the change warrants — to
   `{{ quest.home }}/artifacts/spec/{{ quest.id }}.md`. There is **no separate finalizer
   and no plan**: what you write here is exactly what the implementer builds and the
   reviewer judges against, so make it self-contained and unambiguous.
3. **Go live.** {% include "partials/go-live-worktree.md" %} Then finish the turn with
   `complete_step` (status `Success`).

Make **no code commits** here — you only write the spec, which lives under the quest home,
off the code branch. `save_and_exit` remains available to park the conversation without
building.

## Output

- `status` — `Success` when you go live (spec written, ready to build); `Error` only on
  an unrecoverable failure.
