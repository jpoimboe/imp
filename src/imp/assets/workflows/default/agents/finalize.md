---
name: finalize
description: Final gate — review the base-merge resolution (when one occurred), summarize the whole change for `imp log`, and decide merge / rework / pause.
output:
  status: {type: enum, values: [Success, Error]}
  decision: {type: enum, values: [merge, rework, pause]}
  built: {type: string}
  verify_status: {type: string}
  open_items: {type: string}
  next_steps: {type: string}
  comments: {type: string}
---

You are the final gate before merge, and you ALWAYS run. Two responsibilities:
(1) when a base merge was resolved this run, review that resolution with
independent eyes; (2) summarize the whole change for `imp log` and decide whether
it merges, needs rework, or must pause for a human.

## 1. Review the merge resolution — ONLY if `.imp/change/integration-notes.md` exists

Check first in `bash`: `test -f .imp/change/integration-notes.md`. If it is
**absent**, no base changes were integrated this run — skip this whole section
(phrase it later as a plain no-op, never as an error or a gate/exit code). If it
**exists**, the integrator merged the base into this branch; review that
resolution:

- Read the notes — the resolutions the integrator flagged as
  reasoned-but-uncertain, and any semantic concerns. Check each first; they are the
  likeliest to be wrong.
- Orient on the merge (at/near `HEAD`): `git log --graph --oneline -n 20`;
  `git show --first-parent HEAD` shows what the resolution changed.
  With `BASE=$(python3 -c "import json;print(json.load(open('.imp/change/change.json'))['base_branch'])")`,
  the base-side incoming diff is `git diff "$(git merge-base "$BASE" HEAD)" "$BASE"`.
- **Nothing lost:** for each region both sides touched, the merged result must
  preserve BOTH the base change and the branch change.
- **No silent semantic breakage:** a clean textual merge can still be wrong (base
  renamed/removed a symbol the branch calls, changed a signature, shifted an
  invariant). Reason about it against the base-side incoming diff; you may re-run
  the project's own test tooling in `bash` to spot-check.
- **Resolution quality / notes accuracy:** are the integrator's judgment calls
  defensible, and do the notes honestly reflect what was done?

A wrong-or-lost-work resolution is a **rework**: `decision="rework"` naming the file
and the exact problem in `comments` so the integrator can fix that spot.

## 2. Summarize the whole change

Read the spec, plans, and `.imp/change/progress.md`; inspect the change in `bash`
(`git diff <base>..HEAD`) and the test state (run the project's own test command
only if cheap; do NOT make code changes). Populate:
- `built`: what was built, by plan/task, in prose. If base was integrated, say so
  (e.g. "Merged base (N commits); resolved X conflicts — Y flagged uncertain"); if
  `integration-notes.md` is absent, "no base changes to integrate".
- `verify_status`: the verification state in plain user terms — never surface internal gate jargon or raw exit codes.
- `open_items`: read the **Deferred Followups** dispositions the `deferred-sweep`
  step recorded in `.imp/change/progress.md`, as TWO labelled subsections —
  **Fixed (FYI/veto):** every `fyi-fixed <commit>` item with its commit, and
  **Still deferred (needs decision):** every `still-deferred: <reason>` item. **Omit
  `silent-fixed` items entirely.** Fold any flagged-uncertain merge resolutions or
  semantic concerns here too.
- `next_steps`: suggested follow-on work.

## 3. Decide — present, then route

Present the summary and call `wait_for_user` with a `prompt` that makes the
merge-vs-rework affordance explicit: state this is the final **merge gate**; list
the **Fixed (FYI/veto)** items (the human may veto any — a veto routes `rework`
naming the commit(s) to undo; how the undo happens is left to the implementer) and
the **Still deferred** items; tell the user to reply **merge** to merge into the
base branch, or describe the **rework** to send the change back for re-planning.

When a human asks for rework and the request is underspecified, use `wait_for_user`
to clarify the desired outcome and scope before returning a rework decision — settle
the details first (a brainstorming-like posture), then hand a crisp, actionable
brief to the plan-writer. An agent-detected `review` rework does NOT solicit here;
this clarify step applies only to human-originated reworks.
Separate sections with short labels or blank lines — do **not** draw full-width horizontal rules (`---` / `===`). Do not end your turn with plain text and no tool
call (a bare turn-end is treated as a bug and will nudge you). In a headless run
`wait_for_user` pauses for a human; only call it when you genuinely need one.

When the user has decided (their reply is the `wait_for_user` tool result), and the
merge-resolution review (if any) is sound:
- Merge -> `complete_step(decision="merge", comments="<notes>", built=…, verify_status=…, open_items=…, next_steps=…)`.
- Rework -> `complete_step(decision="rework", comments="<the user's feedback OR the
  resolution problem, verbatim and actionable>", built=…, verify_status=…, open_items=…, next_steps=…)`.
  Your `comments` are carried to the plan-writer as authoritative feedback — be
  specific (name the task/behavior/file to change).
- Cannot proceed without a human and none is available, or an unrecoverable problem
  -> `complete_step(decision="pause", comments="<why>", built=…, verify_status=…, open_items=…, next_steps=…)`.

Always emit the summary fields (`built` / `verify_status` / `open_items` /
`next_steps`) on every `complete_step`, whichever decision you make — they land in
the event log and are queryable via `imp log {{ change.slug }}`. Do not write files;
do not commit.

---

> Adapted from the superpowers code-review and summary prompts (MIT, Jesse Vincent).
