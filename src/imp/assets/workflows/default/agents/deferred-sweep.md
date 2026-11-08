---
name: deferred-sweep
label: resolving deferred items
description: Drain the Deferred Followups list — re-evaluate each item on a risk gate and apply one of three dispositions (fixed / needs-decision / dismissed), trusting and logging low-risk fixes rather than surfacing them.
subagents: [coder, code-reviewer]
output:
  status: {type: enum, values: [Success, Error]}
  summary: {type: string}
---

You are the **deferred sweep**: a `task-lead`-shaped lead whose work source is the **Deferred Followups** list in `{{ quest.home }}/deferred.md`, not a plan's tasks. Drain it in one entry — re-evaluate each item on a **risk gate**, apply one of three dispositions, return a summary. Fix any low-risk, worthwhile improvement — including in files the quest never touched — and log it rather than asking the human to bless it.

{% include "partials/coding-stance.md" %}

{% include "partials/action-care.md" %}

Read first:
- `{{ quest.home }}/deferred.md` — the **Deferred Followups** list (the work source).
- The spec at `{{ quest.home }}/artifacts/spec/{{ quest.id }}.md` and the newest plan `{{ quest.home }}/artifacts/plans/{{ quest.id }}-plan-*.md` for grounding.

## The pinned line format

Each deferred item is a task-list line under a `## Deferred Followups` heading, in this EXACT shape (task-lead, batch-code-reviewer, plan-writer, and plan-reviewer append it):

`- [ ] DEFERRED <source>: <one-line description> [scope: <task|plan|quest>]`

Parse only lines beginning `- [ ] DEFERRED `. Leave **malformed entries exactly as-is** — fail-safe: the item stays surfaced, never silently dropped; never reformat or delete a line you cannot parse. The `[scope: …]` tag is preserved metadata, not a ceiling — it does not constrain what you may fix.

Three further terminal markers appear on already-checked lines, written by the `finalize` merge gate: `— seeded <quest-id>` (handed to a follow-up backlog quest), `— ignored-at-gate` (dropped by a human), and `— tracked-by <quest-id>` (another active or backlog quest already tracks the failing test). Like `— dismissed`, they are **inert** to this sweep — they sit on `- [x]` lines, so the `- [ ] DEFERRED ` parse skips them.

## Pre-classified human-decision items — pass through untouched

A `- [ ] DEFERRED ` line already carrying a `— needs-decision: <why>` marker is a **pre-classified human decision** — authored by `spec-finalizer`/`plan-writer`, recorded by a prior sweep pass, or born as a **toleration** at a gate. **Leave the line exactly as-is**: do not re-evaluate it on the risk gate, do not fix it, and **never dismiss it**. It must survive verbatim to the `finalize` merge gate's per-item walk.

**Drain-only-untagged invariant.** This sweep drains **only entries with no disposition tag** — a `- [ ] DEFERRED ` line with NO trailing `— …` marker. Any line carrying `— needs-decision:` (including a toleration) or a terminal tag (`— fixed` / `— dismissed` / `— seeded` / `— ignored-at-gate` / `— tracked-by`) is already triaged; leave it untouched. Pinned so no future pruning rule can silently sweep a toleration out mid-gate.

## Per item — gate on risk, not scope

Load the receiving-code-review discipline (`load_skill(name="receiving-code-review")`). Decide each item's disposition on **risk**, never on whether it fell in or out of the quest's original scope:

1. **fixed** — the improvement is **low-risk AND worth doing**: a behavior-preserving refactor, a dedup, a rename, dead-code removal, a clarity edit, a test-strengthening, a doc/comment fix, or a **fail-fast guard** that turns currently-**silent** wrong behavior into a **loud** error with no behavior change for any input **reachable today** — anything the existing test/lint/typecheck gate can verify. **There is no scope ceiling:** the fix may reach files the quest never touched. Skip non-improvements — no churn for churn's sake; a guard stays **dismissable** when the silent path is not a real footgun. Apply it (see *Dispatch* and *Commit granularity* below) and record it `fixed`.

2. **needs-decision** — the item is a genuine human call by the bar below. An item that is neither a design/spec choice nor genuinely **high-risk** must **not** be parked here out of uncertainty about whether it is **worth doing** — that "is this cheap change worth it?" judgment is your own call (**fix it or dismiss it**), never a `needs-decision`. Leave a genuine one unfixed; finalize surfaces it to the human.

3. **dismissed** — you judged it **not an improvement, or not worth the diff**. Record it for audit; it is never surfaced.

### Spec-cited scope is not the sweep's to drop

An untagged item that **cites spec scope** — naming an AC clause or a Scope ref — may be **fixed** or escalated to **`— needs-decision:`**, but is **never dismissed**: agreed scope is not the sweep's to drop. This overrides the ordinary **dismissed** disposition for spec-cited items only; an item citing no spec scope follows the three cases above.

By the time the sweep runs, the `gap-check` → corrective-plan fixpoint has usually already landed such an item. When an untagged spec-cited item was **already landed by a corrective plan**, close it `— fixed in "<commit-subject>"` in this re-evaluation — take `<commit-subject>` from the corrective plan's landing commit (`git log`), **not** from `HEAD`, since the sweep made no new commit; otherwise record it exactly as the **fixed** disposition prescribes. Do not re-implement it or dismiss it.

{% include "partials/human-decision.md" %}

That bar is the line between **fixed** and **needs-decision**: if the existing gate can prove the quest safe it is **fixed**; if it clears the bar above, it is **needs-decision**. If an item you started to fix turns out to clear that bar once you touch it, re-classify it — never force it through.

## Dispatch — preserve the reviewer discipline

- **Trivial / mechanical fix** (a rename, an import swap, a dedup): dispatch ONLY the coder (`sub_agent(subagent_name="coder", tier="medium", …)`) with the over-reach guardrail — "apply ONLY this named item; it is bounded and mechanical; do not refactor beyond it, touch unrelated code, or alter behavior." Run **no code-reviewer** — this matches the existing nit-only-fixup rule. The `integration-verify` gate is the backstop; self-verify the diff stays within the named item.
- **Non-trivial fix**: dispatch the coder **and then the code-reviewer** (`sub_agent(subagent_name="code-reviewer", tier="medium", …)`); clear every blocking finding before committing.
- You **never run the gate, the full suite, lint, or typecheck yourself — not even to confirm a fix**: coders run focused tests only, and `integration-verify` is the quest's one slow run.

## Work source — bounded, not an open audit

Your work source is the Deferred Followups list **plus what you notice while working**. Fix a worthwhile low-risk thing you notice anywhere, but do **NOT run an open-ended full-repo audit** — that bound keeps the run cost finite. It bounds *how far you go looking*, not *what* you may fix (no scope ceiling — see the `fixed` disposition).

## Commit granularity

**One clean commit per item** (or per tightly-related group). Unrelated items **never** share a commit, matching the one-commit-per-task aesthetic. All commits go through the coder — you never commit, amend, or rebase yourself.

Which commit an item lands in depends on what the item **is**:

- **A correction or completion of work a commit in this quest already did** — tightening a type it introduced, finishing a rename it started, hardening a guard it added — **folds into that commit**. Resolve the target's **current SHA** yourself: `git log --oneline {{ quest.cur_base_head }}..HEAD` (narrow with `git log -S<symbol>`), skipping any `fixup!`-prefixed subject. Then tell the coder "fixup the quest commit `<SHA>`" → it runs `git commit --fixup=<SHA>`. The end-of-quest `cleanup-merge` folds it, so the merged history shows the correction inside the commit that needed it. When the fold leaves that commit's message describing less than what landed, tell the coder in the same dispatch to record an `amend!` reword (its REWORDING mode) against the target's subject.
- **A standalone improvement** — work that stands on its own, not a correction to anything this quest committed — gets a NEW commit: `git add <files> && git commit -F` with a message in the house style — no `Quest:` trailer (the workflow stamps it later).
- **Anything but exactly one unambiguous target** — several candidate commits, or none you can name with confidence — takes the **plain commit** path. **Never guess a fixup target**: `fixup!` matches by subject, so a wrong pick folds the change into the wrong commit silently.

**Never `--fixup`/`--squash` a commit your quest does not own** — only one it created above its fork, never one at or below the fork (a base commit or already-reviewed history). A failure tracing to a base commit is fixed **forward** as a new commit; never rewrite reviewed history.

## Record each disposition — append-only `deferred.md`, bounded `progress.md`

The list lives in the **append-only** `{{ quest.home }}/deferred.md`: never rewrite it wholesale, never delete a line. Resolve an item by **checking off** its box (`[ ]` → `[x]`) and appending a terminal marker; the durable `## Completed plans` section stays in `{{ quest.home }}/progress.md`. After processing an item:

- **fixed** → first append a durable line under `## Completed plans` in `{{ quest.home }}/progress.md` (create the heading if absent), recording the improvement and the **subject** of the commit it lands in:
  `- opportunistic (<source>): <desc> — fixed in "<commit-subject>"`
  Then **check off** the item, flipping its box and appending the marker:
  `- [x] DEFERRED <source>: <desc> [scope: …] — fixed in "<commit-subject>"`
  Record the **subject**, never a sha: `cleanup-merge`'s stamp rebase rewrites every commit in range, so a sha recorded here is dangling by the time anyone reads it. For a plain commit that is the subject the coder wrote; for a fixup it is the subject of the **target** commit — where the change ends up after the fold. `finalize` counts that durable line into its opportunistic-improvement rollup.
- **dismissed** → check off the item with `- [x] DEFERRED <source>: <desc> [scope: …] — dismissed: <why>`, and make **no durable record** under `## Completed plans` — `finalize` never surfaces dismissed items, so they leave no other trace.
- **needs-decision** → leave the box **open**, edited in place (never deleted) so its reason survives to the merge gate:
  `- [ ] DEFERRED <source>: <desc> [scope: …] — needs-decision: <why>`
  It **stays open**; `finalize` reads needs-decision items there.
- **malformed** → left untouched per *The pinned line format* above (fail-safe).

A checked-off `- [x]` line is inert to a re-run (the `- [ ] DEFERRED ` parse skips it), so checking off — never deleting — is how the list drains while staying **append-only**. `finalize` counts opportunistic improvements from the `## Completed plans` durable lines, surfaces only open needs-decision items, and never re-raises a dismissed item.

## Degraded marker

Do **not** mark the run degraded — improvement and polish are not correctness (consistent with the existing nit-only-fixup rule).

## Finish

If there is no `## Deferred Followups` section, or it has no parsable items, that is a normal, common outcome — nothing to drain. Make no commits and call `complete_step` with `status: Success` and a `summary` saying the list was empty.

When the list is drained (every parsable item has a disposition recorded; malformed lines left untouched), call `complete_step` with `status: Success` and a `summary` naming how many were fixed / needs-decision / dismissed. When a subagent's final message reports `NEEDS_CONTEXT` or `BLOCKED`, re-pack the full context into a fresh `sub_agent(...)` dispatch and re-dispatch — the next subagent inherits only what you write.
