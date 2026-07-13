---
name: summarizer
label: summarizing quest
description: Write the quest's intent-level summary artifact at end-of-quest, sourcing the what from the spec, plans, progress, and diff, and the why from the spec (imp_events fallback).
output:
  status: {type: enum, values: [Success, Error]}
---

Write the quest's **intent-level summary** — what it accomplishes and why, at the level a human operates on. Runs once, near the end of a full-ceremony quest, after the code has been reviewed and settled.

## Source

Write **only** under `{{ quest.home }}/artifacts/`. Do not modify project source, run a build, or make any code change.

Read to ground the summary:
- `{{ quest.home }}/artifacts/spec/{{ quest.id }}.md` — **Goal** and **Decisions summary** are the primary source for the *why*;
- newest plan(s) under `{{ quest.home }}/artifacts/plans/`;
- `{{ quest.home }}/progress.md`;
- `git diff <base>..HEAD` (via `bash`) for the *what*.

When the spec is thin on the *why*, recover the threshold rationale — what was said, not current state — with `imp_events` as a fallback.

## Write

Write `{{ quest.home }}/artifacts/summary/{{ quest.id }}.md`:

- **Headline** — one tight paragraph: what this quest accomplishes and why, intent-level, ruthless signal/noise. No task-by-task enumeration (that lives in `progress.md`, commit messages, and `imp log`).
- **Optional diagram** — text/ASCII only; include **only when the quest genuinely altered structure** (new component, changed data flow); must render in a terminal — no mermaid. Omit when structure did not change.

## Finish

Write only the summary file; the engine journals it. Do not `git add`/`git commit`.

Finish with `complete_step(status="Success")`. On unrecoverable failure use `complete_step(status="Error")` — routes to `finalize` either way; a missing summary is not fatal.
