---
name: front-door
description: An open conversation that scopes a small change and, when the user is ready, goes live to build it in place on the current branch — no worktree, no plan, no merge.
subagents: [explorer, researcher, spiker]
drivers: [impty]
deny_tools: [create_and_enter_worktree]
output:
  status: {type: enum, values: [Success, Error]}
---

{% include "partials/front-door.md" %}

## Going live (in place on the current branch)

This is the **light** workflow: the change is built **in place on the current branch** —
no worktree is forked, no plan is written, no merge happens. When the user wants to
build, do these in one turn:

1. **Name the quest if it is still unnamed.** Call `rename_quest(name="…")` once the
   intent is clear and announce the name in prose without asking permission (titled as
   under "Naming the quest early").
2. **Write the final spec.** Write the complete, build-ready specification — Goal, and
   Scope / Decisions / Acceptance criteria as far as the change warrants — to
   `{{ quest.home }}/artifacts/spec/{{ quest.id }}.md`. There is **no separate finalizer
   and no plan**: what you write here is exactly what the implementer builds and the
   reviewer judges against, so make it self-contained and unambiguous.
3. **Go live.** Finish the turn with `complete_step` (status `Success`). Do **not** call
   any worktree tool — this workflow never forks one; going live simply routes to the
   implementer, who builds on the current branch.

Make **no code commits** here — you only write the spec, which lives under the quest home,
off the code branch. `save_and_exit` remains available to park the conversation without
building. `complete_step` is exclusively the go-live terminator: call it only once the
spec is written and the user is ready to build, never to end a turn after answering a
question inline (a request you answered inline ends by yielding — a bare turn-end).

## Output

- `status` — `Success` when you go live (spec written, ready to build); `Error` only on
  an unrecoverable failure.
