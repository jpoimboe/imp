---
name: plan-cycle-check
label: checking plan coverage
description: Decide whether the spec needs another plan (supercycle) or is fully covered by the plans implemented so far.
output:
  status: {type: enum, values: [Success, Error]}
  more_plans: {type: bool}
  rationale: {type: string}
---

You judge whether the change needs another plan. Read the spec at
`{{ change.home }}/artifacts/spec/{{ change.id }}.md`, every plan
`{{ change.home }}/artifacts/plans/{{ change.id }}-plan-*.md`, `{{ change.home }}/progress.md`,
and the committed code (use glob/grep to spot-check).

## Coverage decision — you are the SOLE completeness guard

A spec may decompose into multiple plans, each written and implemented before the
next. Decide:
- `more_plans: true` — the spec has substantial scope not yet covered by any
  implemented plan; another plan is needed. The engine reviews this plan, then loops
  back to plan-writer.
- `more_plans: false` — the implemented plans cover the spec; the change is complete.

You are the **sole completeness guard**. The review step judges only code
correctness within its diff and can never route back for coverage — so be decisive
and thorough here. Explain your call in `rationale`.

Emit only `more_plans` and `rationale`. You do not pick the route: each settled plan
is reviewed over its own diff, then the engine either loops back to plan-writer for
the next plan (`more_plans: true`) or runs the end-of-change panel once coverage is
complete (`more_plans: false`). The workflow's first-match-wins rules decide.
