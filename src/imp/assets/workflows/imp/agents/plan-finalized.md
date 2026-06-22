---
name: plan-finalized
label: finalizing plan
description: Announce the just-finalized plan's task list once and record its task count.
output:
  status: {type: enum, values: [Success, Error]}
  task_total: {type: int}
---

A plan has just been finalized and is about to be implemented. Announce its task
list once, and report how many tasks it contains. You change no files and run no
tests — this step only announces and counts.

## Steps

1. Find the current plan: glob
   `{{ change.home }}/artifacts/plans/{{ change.id }}-plan-*.md` and pick the
   highest-numbered file (the just-finalized plan).
2. Count its `## Task` headings — that count is `task_total`.
3. Read each `## Task N: <title>` heading to get the per-task titles.
4. Call `emit_notice` exactly ONCE with a single block of this shape — three
   leading spaces before each numbered line so the renderer indents the list under
   the header:

   ```
   ✓ plan {{ vars.plan_index }}{% if vars.plan_total %}/{{ vars.plan_total }}{% endif %} · <count> tasks
      1. <task 1 title>
      2. <task 2 title>
      …
   ```

   Replace `<count>` with the task count and each `<task N title>` with the title
   from the matching `## Task N: <title>` heading. Pass the whole block as the
   single `text` argument.
5. Return `task_total` = the task count and `status: Success`.
