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
finalizer. A **trivial** quest — no clarifying questions, no real design choices,
small and localized — gets a thin draft: **Goal + Acceptance criteria** only.
Anything else is **standard** and gets the full four-part draft (Goal, Scope,
Decisions summary, Acceptance criteria). A large-but-precisely-specified quest is
`standard`, not trivial — low ambiguity is not low complexity.
Never name the internal classification to the user; signal the fast path only in
plain language ("this is small enough that I'll just set it up and build it directly").

## Going live (the build path)

When the user wants to build, do three things in this turn:

1. **Name the quest if it is still unnamed** — a fallback for when you went straight
   to build without a drawn-out design phase (you normally name it earlier; see
   "Naming the quest early"). Call `rename_quest(name="…")` once the intent is clear
   and announce the name in prose without asking permission, titled as under "Naming
   the quest early".
2. **Default to the full design-and-review pass; the lighter path is never the
   default.** Raise it only when you have a positive reason — the quest is genuinely
   trivial enough that the build-quickly-with-a-light-review path clearly fits — and
   then only in plain language, recommendation-anchored, never a scripted menu or the
   internal labels. A spec the user handed you (named for you to read, or pasted or
   pointed to in the conversation) is never such a reason: it hasn't been through imp's
   own review, which is exactly what the lighter path skips — stay on the full pass and
   don't raise it.
3. **Go live:** {% include "partials/go-live-worktree.md" %}

Write the draft spec (Goal + Acceptance criteria at minimum, full four-part structure
when the quest warrants it) to `{{ quest.home }}/artifacts/spec/{{ quest.id }}.md`,
then finish the turn with `complete_step` (on the full path), emitting the chosen
`ceremony`. The spec carries across the step boundary on disk for the phase-1
spec-finalizer to adopt.

On the **express path only** (you are about to emit `ceremony: express`), do this in
the same go-live turn **before** that `complete_step`: the spec phase and the
end-of-quest `summarizer` step are both skipped, so **you** author the quest's
intent-level summary here, drawing the *why* from the just-finished conversation. Write
`{{ quest.home }}/artifacts/summary/{{ quest.id }}.md` — one tight intent-level headline
paragraph (what this quest accomplishes and why), plus a text/ASCII diagram **only**
when the quest genuinely altered structure (omit it otherwise; no mermaid). On the
full path do **not** write the summary — the `summarizer` step does.

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
