{# Read-only-step note. Injected only when the agent holds a filesystem tool and the resolved sandbox policy denies writing its own worktree, immediately after the paths block. #}

**This step is read-only.** Your worktree is bound read-only — `write_file` /
`edit_file` and any shell command that writes inside it will be refused. Only the
worktree: a write your instructions direct elsewhere, such as your quest home's
`deferred.md`, is unaffected. Inspect and report; don't change the worktree.
