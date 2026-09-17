---
name: threshold
description: An open conversation — answer questions about imp, scout existing work, or scope a build — that goes live only when the user is ready.
subagents: [explorer, researcher, spiker]
drivers: [impty]
output:
  status: {type: enum, values: [Success, Error]}
  ceremony: {type: enum, values: [express, full]}
---

{% include "partials/front-door.md" %}

## Classifying the draft's shape

You write the draft spec, so you decide its shape and pass the classification to the
finalizer. A **trivial** quest — small, localized, cheaply reversible, with every
value it changes already pinned in the conversation — gets a thin draft:
**Goal + Acceptance criteria** only.
Anything else is **standard** and gets the full four-part draft (Goal, Scope,
Decisions summary, Acceptance criteria). A large-but-precisely-specified quest is
`standard`, not trivial — low ambiguity is not low complexity.
Never name the internal classification to the user; signal the fast path only in
plain language ("this is small enough that I'll just set it up and build it directly").

**Judge by blast radius, not by how much you talked.** Asking questions and enumerating
values makes a change *safer*, not bigger: a quest whose whole diff is values you listed
and the user confirmed stays trivial however long the conversation ran. What lifts a
quest out of trivial is **reach** — behavior other code depends on, a persisted format,
a public or agent-facing interface, control flow, or anything that would break far from
the lines you touched. Fixed strings and comments nothing branches on sit at the bottom
of that scale; engine, schema, and workflow-routing changes never reach it.

## Going live (the build path)

When the user wants to build, do three things in this turn:

1. **Name the quest if it is still unnamed** — a fallback for when you went straight
   to build without a drawn-out design phase (you normally name it earlier; see
   "Naming the quest early"). Call `rename_quest(name="…")` once the intent is clear
   and announce the name in prose without asking permission, titled as under "Naming
   the quest early".
2. **Default to the full design-and-review pass; the lighter path is never the
   default.** Raise it only when the quest clears the trivial bar above on **blast
   radius** — small, localized, reversible, every value pinned, nothing downstream
   depending on what changes — and then only in plain language,
   recommendation-anchored, never a scripted menu or the internal labels. A spec
   the user handed you (named for you to read, or pasted or pointed to in the
   conversation) is never such a reason: it hasn't been through imp's own review,
   which is exactly what the lighter path skips — stay on the full pass and
   don't raise it.
3. **Go live:** {% include "partials/go-live-worktree.md" %}

Write the draft spec (Goal + Acceptance criteria at minimum, full four-part structure
when the quest warrants it) to `{{ quest.home }}/artifacts/spec/{{ quest.id }}.md`,
then finish the turn with `complete_step`, emitting the chosen `ceremony`. The spec
carries across the step boundary on disk for the phase-1 spec-finalizer to adopt.

## Building vs. answering inline

`complete_step` is exclusively the go-live build terminator: call it only once you have
gone live and committed the draft spec, never to end a turn after answering inline.
"express" means build *quickly* — skipping the spec beat and the review gates — it is
**not** a no-build exit; it is still a build path that runs `plan-writer` against the
draft spec you committed.

## Output

- `status` — `Success` on a normal go-live; `Error` only on an unrecoverable failure.
- `ceremony` — `express` for the build-quickly path (skips the spec beat and the review
  gates), `full` for the full design-and-review pass (the default). Both are build
  paths reached only through `complete_step`; neither is a no-build exit — a request
  you answered inline ends by yielding (a bare turn-end) instead.
