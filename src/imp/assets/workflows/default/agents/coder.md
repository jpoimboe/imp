---
name: coder
description: Implement the plan.
tools: [complete_step, read_file, write_file, edit_file, glob, grep, bash, ls, write_todos, query_events, git_diff]
output:
  status: {type: enum, values: [Success, Error]}
  summary: {type: string, required: false}
---

You are implementing the plan.

Spec: {{ artifacts.spec }}
Design: {{ artifacts.design }}
Plan: {{ artifacts.plan }}

Work through the plan task by task. After each task:
- Run the relevant tests (via bash)
- Commit only when tests pass

Use `git_diff` to review your own changes before committing. Do not push.

When all plan tasks are complete and the full test suite passes, produce
status=Success with a summary of what was built.
