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
or verify flagged, and your recommendation. Then solicit ONE decision via
`wait_for_user`:
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
