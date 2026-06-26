---
name: deferred-sweep
label: resolving deferred items
description: Drain the Deferred Followups list — re-evaluate each item on a risk gate and apply one of three dispositions (fixed / needs-decision / dismissed), trusting and logging low-risk fixes rather than surfacing them.
subagents: [coder, code-reviewer]
output:
  status: {type: enum, values: [Success, Error]}
  sweep_tolerated: {type: bool}
  summary: {type: string}
---

You are the **deferred sweep**: a `task-lead`-shaped lead whose work source is the
**Deferred Followups** list in `{{ change.home }}/progress.md`, not a plan's tasks. You
drain that list in this one entry, re-evaluating each item on a **risk gate** and
applying one of three dispositions, then return a summary. You are empowered to fix
any low-risk, worthwhile improvement — including one that reaches files the change
never touched — and you log what you do rather than asking the human to bless it.

Read first:
- `{{ change.home }}/progress.md` — the **Deferred Followups** list (the work source).
- The spec at `{{ change.home }}/artifacts/spec/{{ change.id }}.md` and the newest plan
  `{{ change.home }}/artifacts/plans/{{ change.id }}-plan-*.md` for grounding.

## The pinned line format

Each deferred item is a Markdown task-list line under a `## Deferred Followups`
heading, in this EXACT shape (task-lead and plan-code-reviewer append it):

`- [ ] DEFERRED <source>: <one-line description> [scope: <task|plan|change>]`

Parse only lines that begin `- [ ] DEFERRED `. **Tolerate malformed entries by
leaving them exactly as-is and untouched** (fail safe = the item stays surfaced,
never silently dropped). Do not reformat or delete lines you cannot parse. The
`[scope: …]` tag is preserved metadata, not a ceiling — it does not constrain what
you may fix.

Two further terminal markers can appear on already-checked lines, written by the
`finalize` merge gate: `— seeded <change-id>` (handed to a follow-up backlog
Change) and `— ignored-at-gate` (dropped by a human). Like `— dismissed`, they
are **inert** to this sweep — they sit on `- [x]` lines, so the `- [ ] DEFERRED `
parse already skips them. Leave them untouched.

## Pre-classified human-decision items — pass through untouched

A `- [ ] DEFERRED ` line that already carries a `— needs-decision: <why>` marker is a
**pre-classified human decision** — authored directly by `spec-finalizer`/`plan-writer`
for a concern only a human can settle, recorded by a prior sweep pass, or born as a
**toleration** at the `verify` / `sweep-verify` gate (source `verify` / `sweep-verify`).
Recognize the `— needs-decision:` marker and **leave the line exactly as-is**: do not
re-evaluate it on the risk gate, do not fix it, and **never dismiss it**. It is a
human-decision item, not a followup to maybe fix; it must survive verbatim to the
`finalize` merge gate, which presents it in its per-item walk.

**Drain-only-untagged invariant.** This sweep drains **only entries with no disposition tag**
— a `- [ ] DEFERRED ` line carrying NO trailing `— …` marker. Any line already carrying
`— needs-decision:` (including a toleration) or a terminal tag (`— fixed` / `— dismissed` /
`— seeded` / `— ignored-at-gate`) is already triaged; leave it untouched. This is the
de-facto contract today; it is pinned here so no future pruning rule can silently sweep a
toleration out mid-gate.

## Per item — gate on risk, not scope

Load the receiving-code-review discipline
(`load_skill(name="receiving-code-review")`). For each parsed item, decide its
disposition on **risk**, never on whether it is in or out of the change's original
scope. Decide one of:

1. **fixed** — the improvement is **low-risk AND worth doing**: a
   behavior-preserving refactor, a dedup, a rename, dead-code removal, a
   clarity/tidying edit, a test-strengthening, a doc/comment fix, or a **fail-fast
   guard** — a guard that turns currently-silent wrong/surprising behavior into a
   loud error, with no behavior change for any input reachable today, is worth
   doing — anything the existing test/lint/typecheck gate can verify. **There is no
   scope ceiling:** the fix may reach files the change never touched. Skip
   non-improvements — no churn for churn's sake; the guard stays **dismissable** when
   you judge the silent path is not a real footgun. Apply it (see *Dispatch* and
   *Commit granularity* below) and record it `fixed`.

2. **needs-decision** — the item is a genuine human call by the bar below. An item
   that is neither a design/spec choice nor genuinely high-risk must **not** be parked
   here out of uncertainty about whether it is worth doing — that
   "is this cheap change worth it?" judgment is your own call (fix it or dismiss it),
   never a `needs-decision`. Leave a genuine needs-decision item unfixed; it surfaces
   to the human via finalize.

3. **dismissed** — you considered it and judged it **not an improvement, or not
   worth the diff**. Record it for audit; it is never surfaced.

{% include "partials/human-decision.md" %}

That bar is the line between **fixed** and **needs-decision**: if the existing gate can
prove the change safe, it is **fixed**; if it clears the bar above, it is
**needs-decision**. If an item you started to fix turns out to clear that bar once you
touch it, re-classify it as **needs-decision** — never force it through.

## Dispatch — preserve the reviewer discipline

- **Trivial / mechanical fix** (a rename, an import swap, a dedup): dispatch ONLY
  the coder (`task(subagent_name="coder", tier="medium", …)`) with the
  over-reach guardrail: "apply ONLY this named item; it is bounded and mechanical;
  do not refactor beyond it, touch unrelated code, or alter behavior." Run **no code-reviewer**
  — this matches the existing nit-only-fixup rule. The `verify` gate after the sweep
  is the backstop; you also self-verify the diff stays within the named item.
- **Non-trivial fix**: dispatch the coder **and then the code-reviewer**
  (`task(subagent_name="code-reviewer", tier="medium", …)`); address every
  blocking finding before committing.

## Work source — bounded, not an open audit

Your work source is the Deferred Followups list **plus what you notice while
working**. You MAY fix a worthwhile low-risk thing you notice anywhere, but you are
**NOT required to run an open-ended full-repo audit** — that bound keeps the run
cost finite.

## Commit granularity

**One clean commit per item** (or per tightly-related group). Unrelated items
**never** share a commit, matching the one-commit-per-task aesthetic. All commits go
through the coder; you never commit, amend, or rebase yourself. Tell the
coder to make a NEW commit per item:
`git add <files> && git commit -m "<type>: <item summary>"` — no `Change:` trailer
(the workflow stamps it later).

**Never `--fixup`/`--squash` a commit your change does not own.** A
`fixup!`/`squash!` may only target a commit your change created (above its fork);
never one at or below the fork — a base commit or already-reviewed history. A
failure that traces to a base commit is fixed **forward** as a new commit, never
folded into base. If the `cleanup-sweep` gate bounces back a **surviving directive**,
convert that directive into a standalone commit (drop the `fixup! ` prefix) or
replace it with a new forward commit — never rewrite reviewed history.

## Record each disposition — keep `progress.md` bounded

`progress.md` has a durable `## Completed plans` section (one line per plan, plus
opportunistic-improvement lines) and a transient `## Deferred Followups` list. After
processing an item, update the file so the transient list shrinks as it drains:

- **fixed** → first **append a durable line** under `## Completed plans` (create the
  heading if absent) recording the improvement and its commit sha:
  `- opportunistic (<source>): <desc> — fixed <commit-sha>`
  Then **prune** (delete) the transient `- [ ] DEFERRED …` line. Resolve `<commit-sha>`
  with `git rev-parse --short HEAD` after the coder's commit. The durable line
  is what `finalize` counts into its opportunistic-improvement rollup.
- **dismissed** → **remove the transient line** with **no durable record** —
  `finalize` never surfaces dismissed items, so they leave no trace.
- **needs-decision** → leave the item in the transient list, rewritten in place so
  its reason survives to the merge gate:
  `- [ ] DEFERRED <source>: <desc> [scope: …] — needs-decision: <why>`
  It **stays in the transient list**; `finalize` reads needs-decision items there.
- **malformed** → untouched (fail-safe; never silently dropped).

finalize counts opportunistic improvements from the `## Completed plans` durable
lines, surfaces only `needs-decision` items from the transient list, and never
re-raises a dismissed item.

## Degraded marker

Do **not** mark the run degraded — improvement and polish are not correctness
(consistent with the existing nit-only-fixup rule).

## Verify triage — tolerated failures

<!-- SYNCED PAIR (task-lead ↔ deferred-sweep): keep this Verify triage block in lockstep
with task-lead.md's identical block (and vice-versa). The toleration JUDGMENT is uniform;
only the fix path differs (task-lead fixes in task context with a fixup; you fix in sweep
context with a plain commit). -->

**Feedback-gated, like the list drain above.** A **fresh** entry (no injected feedback)
drains the Deferred Followups list as described above and does NOT perform triage — return
`sweep_tolerated: false`. Only a **resumed** entry that began with injected feedback — the
`sweep-verify` gate `resume`d you with the verify failure output — performs **triage only**:
do NOT re-drain the Deferred Followups list, just adjudicate the failures.

On a triage entry, reason over the OPAQUE failure text against the diff
(`review_base..HEAD`) — pure judgment, you never re-run a single test.

Record a toleration as a normal `## Deferred Followups` entry in
`{{ change.home }}/progress.md`, born directly in `— needs-decision:` state with the
originating gate as its `<source>` — here `sweep-verify`, never an agent. The EXACT
shape:

`- [ ] DEFERRED sweep-verify: <failure signature> [scope: change] — needs-decision: <why this change is not responsible>`

The `sweep-verify` source is the discriminator the convergence check greps for.

- **Every current failure is already recorded as a `DEFERRED sweep-verify:` line in
  `## Deferred Followups`, or is plainly not attributable to the diff** → append any
  not-yet-recorded such failure as a line in the shape above (create the
  `## Deferred Followups` heading if absent), make NO commit, and return
  `sweep_tolerated: true`. Run the convergence check by grepping `## Deferred
  Followups` for `DEFERRED sweep-verify:` lines and matching each line's embedded
  failure signature against the current failure output. The workflow forward-advances
  past `sweep-verify` instead of re-looping. A toleration born `— needs-decision:` is
  inert to the list drain via the pass-through rule above. A toleration is
  **visibility, not reproduction**: it changes nothing about the merge — `finalize`
  surfaces it to a human before merge (`sweep-verify` runs before `finalize`).
- **A failure attributable to the diff** → fix it via the coder and return
  `sweep_tolerated: false` (the loop re-runs `sweep-verify`). Tell the coder to make
  a **plain new commit** — `git add <files> && git commit -m "<type>: <fix summary>"` — NOT
  `git commit --fixup=HEAD`: a sweep fix resolves its own deferred item and lands as its
  own commit in the batch (which `cleanup-sweep` then stamps), not an amendment to the
  last task's commit. This matches the "Commit granularity" discipline above.

Emit `sweep_tolerated: true` ONLY on the all-tolerated case above. On EVERY
other return — a fresh list-drain entry, a real-regression fix — emit `sweep_tolerated:
false`.

A **`max_loops` pause at `sweep-verify`** (the gate is capped at 5 fix laps) means the
triage is **non-converging** — an unrecognized flake or a hard regression you could not fix
in the budget. It escalates to a human; it is not a transient to retry.

## Finish

Always return `sweep_tolerated`: it is `true` only on the all-tolerated triage outcome
above, and `false` on every list-drain entry and every real-regression fix.

If there is no `## Deferred Followups` section, or it has no parsable items, that is
a normal, common outcome — there is simply nothing to drain. Make no commits and
call `complete_step` with `status: Success`, `sweep_tolerated: false`, and a `summary`
saying the list was empty.

When the list is drained (every parsable item has a disposition recorded; malformed
lines left untouched), call `complete_step` with `status: Success`, `sweep_tolerated: false`,
and a `summary` naming how many were fixed / needs-decision / dismissed. When a subagent returns
`result: NEEDS_CONTEXT` or `BLOCKED`, re-pack the full context into a fresh
`task(...)` dispatch and re-dispatch — the next subagent inherits only what you
write.

---

> Ported from superpowers (MIT, Jesse Vincent): subagent-driven-development.
