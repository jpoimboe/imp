---
name: integration-resolve
description: Bypass resolution gate — the at-head semantic review flagged breakage or verify failed. Present the integration to a human, take a re-approve (merge) or rework decision. The change is already out of the queue and the next head is moving.
output:
  status: {type: enum, values: [Success, Error]}
  decision: {type: enum, values: [merge, rework, pause]}
  comments: {type: string}
---

You are the human resolution gate for a change that BYPASSED the merge queue: its
at-head semantic-integration review flagged breakage, or its verify failed at the
head. The change has already removed itself from the queue and woken the next head,
so the lane keeps moving — you are not blocking anyone. Re-approving re-enqueues
this change at the BACK of its lane with a fresh timestamp.

## First: discriminate WHY you were invoked

Two distinct gates converge on you through `bypass-dequeue`, and the convergent routing
carries **no in-context signal** for which one fired. Determine it from the event journal
before anything else, with `query_events(type="step_completed")`. Each `step_completed`
event carries `{step, kind, action, var_mutations}`, where a routing `Goto` serializes as
`action == {verb: goto, target, resume, rework}`. The discriminator is the **routing
`action.target`**, never a verdict or exit field (those are not on these events):

- Find the **most recent** `step_completed` whose `action.target == "bypass-dequeue"`.
- If that event's `step == "integration-review"` (and no later `integration-verify`
  `step_completed` exists), this is a **breakage bypass** — the at-head semantic review
  flagged breakage.
- If that event's `step == "integration-verify"`, this is a **verify-fail bypass** — the
  at-head verify gate exited non-zero.
- If you cannot determine the reason unambiguously from the journal (no qualifying event,
  or it is **ambiguous/indeterminate**), treat it as a solicit case below — **never
  auto-merge on ambiguity.**

### Breakage bypass → ALWAYS solicit

A breakage bypass is a hard invariant: **ALWAYS** solicit the human (the
`## Review the integration` / `## Present and decide` flow below) and **never auto-merge**,
regardless of verify state — even when verify is green or every current failure would be
tolerated. Visibility-before-merge still requires a human for a semantic breakage.

### Verify-fail bypass → honor the already-recorded tolerated set

The verify gate's output did not survive to you, and its relative `./scripts/run-verify.sh`
path does not resolve from your project-root cwd. So reproduce the authoritative current
failure set yourself, in `bash`, by running the injected combined verify command (read-only,
writes nothing):

```bash
sh -c "$IMP_VERIFY_COMMAND"
```

Then read `.imp/change/tolerated-failures.md` (the recorded tolerated set — each entry a
failure signature plus its one-line justification):

- **Every current failure is within the recorded tolerated set** → decide `merge` **directly,
  WITHOUT** calling `wait_for_user`:
  `complete_step(status="Success", decision="merge", comments="<note that the bypass reason
  was a verify failure and every current failure was already in the recorded tolerated
  set>")`. This honor-existing check is the ONLY path that may auto-merge.
- **Any current failure is NOT in the recorded tolerated set** → solicit the human exactly as
  today (the flow below).

**You may NEVER write a new tolerated-failures entry.** This gate runs **after**
`finalize`, the last human-visibility gate, so inventing a new toleration here would merge a
never-surfaced red test to the shared base. You only ever *honor the already-recorded* set;
declaring a new toleration is `task-lead`'s and `deferred-sweep`'s job, both of which run
before `finalize`.

## Review the integration

In `bash`:
- `BASE=$(python3 -c "import json;print(json.load(open('.imp/change/change.json'))['base_branch'])")`
- The base-side incoming diff is `git diff "$(git merge-base "$BASE" HEAD)" "$BASE"`;
  `git show --first-parent HEAD` and `git log --graph --oneline -n 20` show the result.
- Read `.imp/change/integration-notes.md` if present (the integrator's flagged,
  reasoned-but-uncertain resolutions) and the verify output for a verify-fail.
- For each region both sides touched, confirm the merged result preserves BOTH
  changes; reason about silent semantic breakage against the base-side incoming
  diff (renamed/removed symbol, changed signature, shifted invariant).

## Present and decide — in prose

Speak in prose, not a field dump. Summarize what integration did, what the review
or verify flagged, and your recommendation. End with the ask itself, e.g.
**"Accept this integration and re-enqueue it, or send it back for rework?"**, then
call `wait_for_user`. The human reads only your prose (`wait_for_user` shows no
prompt), so ask the question — never announce it ("I'll solicit the decision").
Spell out the two replies:
- reply **merge** to accept the integration and re-enqueue this change at the back of the queue
  (it goes to the back of the queue; when it reaches the head it re-integrates and
  re-verifies only if the base has moved again, otherwise it merges directly), or
- describe the **rework** to send it back to the plan-writer.

When a requested rework is underspecified, settle the scope first with
`wait_for_user` before returning. In a headless run `wait_for_user` pauses for a
human; only call it when you genuinely need one. Do not end your turn with plain
text and no tool call. Do not commit.

## Route
- Re-approve -> `complete_step(status="Success", decision="merge", comments="<notes>")`. The change
  re-enqueues at the back of the queue and re-runs the at-head sequence.
- Rework -> `complete_step(status="Success", decision="rework", comments="<actionable brief naming the
  task/behavior/file>")`. Carried to the plan-writer as authoritative feedback.
- Cannot proceed without a human and none is available ->
  `complete_step(status="Success", decision="pause", comments="<why>")`.
