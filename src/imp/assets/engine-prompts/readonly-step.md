{# Read-only-step note. Injected only when the agent holds a filesystem tool and the resolved sandbox policy denies writing its own worktree, immediately after the paths block. #}

**This step is read-only.** Your worktree is bound read-only — `write_file` /
`edit_file` and any mutating shell command will be refused. Inspect and report; don't
attempt changes.
