# imp operator notes

Runbook for operating a running imp change.

## Unblocking a paused change

- A running change executes from a **gitignored `.imp/change/workflow`
  snapshot** that shadows its branch (`change.py` — `workflow_dir` returns the
  snapshot when present, else `live_workflow_dir`). Engine *code* fixes deploy on
  ff-merge (editable pipx install → `main`'s `src/`); workflow *assets and
  scripts* do not — the snapshot wins.
- **Consequence:** cherry-picking a workflow fix onto a paused change's branch is
  **inert** when the change has a snapshot (the normal case). To unblock, edit
  the snapshot directly (`.imp/change/workflow/workflow.yaml` plus `scripts/`); a
  config fix goes in the gitignored local layer `<worktree>/.imp/config.yaml`.
  The old cherry-pick-onto-branch recipe only works when there is *no* snapshot.
- **Skew deadlock:** the snapshot is taken once and never auto-refreshed (the
  `--refresh-workflow` re-snapshot is unbuilt — `executor.py` references it only
  as a hypothetical). A change that edits its own `plan-writer` / `spec-writer`
  *and* adds an invariant runs the OLD writers while `review` enforces the NEW
  invariant, producing an infinite remove/restore loop; `max_loops` is the only
  backstop. Recognize it when a change loops on "removing/restoring its own
  plan/workflow file"; unblock by editing the snapshot.
