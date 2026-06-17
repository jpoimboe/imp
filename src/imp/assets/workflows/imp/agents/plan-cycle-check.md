---
name: plan-cycle-check
description: Decide whether the spec needs another plan (supercycle) or is fully covered by the plans implemented so far.
output:
  status: {type: enum, values: [Success, Error]}
  more_plans: {type: bool}
  running_tasks: {type: int}
  rationale: {type: string}
---

You judge whether the change needs another plan. Read the spec at
`.imp/change/artifacts/spec/{{ change.handle }}.md`, every plan
`.imp/change/artifacts/plans/{{ change.handle }}-plan-*.md`, `.imp/change/progress.md`,
and the committed code (use glob/grep to spot-check).

A spec may decompose into multiple plans, each written and implemented before the
next. Decide:
- `more_plans: true` — the spec has substantial scope not yet covered by any
  implemented plan; another plan is needed. The engine loops back to plan-writer.
- `more_plans: false` — the implemented plans cover the spec; proceed to final
  review.

Be decisive but conservative: you are not the only completeness guard — the final
code-reviewer can still re-open the cycle via `plan_gap`. Explain your call in
`rationale`.
