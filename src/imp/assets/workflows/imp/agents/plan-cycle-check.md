---
name: plan-cycle-check
description: Decide whether the spec needs another plan (supercycle) or is fully covered by the plans implemented so far.
output:
  status: {type: enum, values: [Success, Error]}
  more_plans: {type: bool}
  running_tasks: {type: int}
  rationale: {type: string}
---

You judge whether the change needs another plan, AND you advance the review
cadence counter. Read the spec at
`.imp/change/artifacts/spec/{{ change.handle }}.md`, every plan
`.imp/change/artifacts/plans/{{ change.handle }}-plan-*.md`, `.imp/change/progress.md`,
and the committed code (use glob/grep to spot-check).

## 1. Coverage decision — you are the SOLE completeness guard

A spec may decompose into multiple plans, each written and implemented before the
next. Decide:
- `more_plans: true` — the spec has substantial scope not yet covered by any
  implemented plan; another plan is needed. The engine loops back to plan-writer.
- `more_plans: false` — the implemented plans cover the spec; proceed to the
  end-of-change review.

You are the **sole completeness guard**. The review step judges only code
correctness within its diff and can never route back for coverage — so be decisive
and thorough here. Explain your call in `rationale`.

## 2. Advance the review counter — `running_tasks`

The engine fires review on an accumulated-task cadence and uses your
`running_tasks` value as the counter. Compute it deterministically:

1. Take the current counter: `{{ vars.tasks_since_review }}` (0 at change start, or
   the value carried since the last review fired).
2. Count the tasks in the **just-implemented plan file** — the newest
   `.imp/change/artifacts/plans/{{ change.handle }}-plan-*.md` (highest N). The plan
   file is the authoritative task list (count its `## Task` sections); do NOT count
   from `progress.md`, whose per-task narrative is intentionally bounded.
3. Emit `running_tasks` = the sum.

The engine compares `running_tasks` against the batch threshold and routes
accordingly (per-batch review when it crosses the threshold; the end-of-change
panel when coverage is complete; otherwise the next plan). You do not pick the
route — emit `more_plans`, `running_tasks`, and `rationale`, and let the workflow's
first-match-wins rules decide.
