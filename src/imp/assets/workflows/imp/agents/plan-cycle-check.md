---
name: plan-cycle-check
label: checking plan coverage
description: Decide whether the spec needs another plan (supercycle) or is fully covered by the plans implemented so far.
output:
  status: {type: enum, values: [Success, Error]}
  more_plans: {type: bool}
  plan_total: {type: int}
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

Emit `more_plans`, `plan_total`, and `rationale`. You do not pick the route: each
settled plan is reviewed over its own diff, then the engine either loops back to
plan-writer for the next plan (`more_plans: true`) or runs the end-of-change panel
once coverage is complete (`more_plans: false`). The workflow's first-match-wins
rules decide.

## Forward notes — the two `more_plans: true` triggers

`task-lead` now flows forward and logs what it could not fix as durable notes in
`{{ change.home }}/progress.md`. Read those notes (they live in the durable region
alongside `## Deferred Followups`, surviving the end-of-plan collapse) and emit
`more_plans: true` in exactly two cases:

1. **Open needs-replan note.** A `- [ ] NEEDS-REPLAN task-lead: …` checkbox that is
   still **open** (`[ ]`). Once `plan-writer` authors the covering `N+1` it marks the
   box `[x]`, so a consumed note never re-fires and cannot drive an unbounded
   supercycle — only an open box triggers a plan.
2. **Independent coverage backstop.** Re-derive coverage from the spec + committed
   code and find real *code* scope still uncovered that is **not** already accounted
   for — i.e. not parked under a `— needs-decision:` item and not closed by a
   `resolved-forward` note.

Subtleties:

- A **`— needs-decision:` item suppresses the backstop for its scope.** A gap that
  exists only because a human must still rule on it must not trigger a plan; it rides
  to the `finalize` gate. If the human says "build it," that becomes a finalize-rework
  that authors the plan then.
- A `resolved-forward` note is informational only — `task-lead` already fixed it and
  the tree is green, so it never drives coverage.
- The backstop can only override **upward** (force a plan when `task-lead` mis-marked
  something resolved-forward), never downward: it never suppresses a plan that an open
  needs-replan note demands.

You still never call `wait_for_user` and never author a `— needs-decision:` item;
your sole lever is `more_plans`.

## Anticipated plan total — `plan_total`

You are the per-lap **refiner** of `plan_total`. `plan-writer` seeds the first value
alongside plan 1 (`plan_index == 1`); from your first lap onward you refine and clamp it,
so the rendered `plan N/total` position is never dishonest. Emit your honest anticipated
number of plans for the whole spec, already clamped:

- `plan_total` MUST be `>= {{ vars.plan_index }}` (the current plan number).
- When `more_plans` is **true** (another plan follows), `plan_total` MUST be
  **strictly greater** than `{{ vars.plan_index }}` — at least `{{ vars.plan_index }} + 1` —
  because the engine advances the plan counter as it loops back to plan-writer.
- When `more_plans` is **false** (coverage complete), emit
  `plan_total == {{ vars.plan_index }}` exactly.

plan-writer's first-plan seed is only a starting estimate; refine it on every lap you run.
