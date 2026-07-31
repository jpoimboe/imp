# imp operator notes

Runbook for operating a running imp quest.

## Unblocking a paused quest

- A running quest executes from a **workflow snapshot** in its quest home
  (`~/.imp/projects/<slug>/quests/<id>/workflow/`, which the quest's worktree
  reaches as `.imp/quests/<id>/workflow/`) that shadows its branch (`quest.py` —
  `workflow_dir` returns the snapshot when present, else `live_workflow_dir`).
  Engine *code* fixes deploy on ff-merge (editable pipx install → `main`'s
  `src/`); workflow *assets and scripts* do not — the snapshot wins.
- **Consequence:** cherry-picking a workflow fix onto a paused quest's branch is
  **inert** when the quest has a snapshot (the normal case). To unblock, edit
  the snapshot directly (`.imp/quests/<id>/workflow/workflow.yaml` plus
  `scripts/`); a config fix goes in the main checkout's local layer
  `~/.imp/projects/<slug>/config.yaml` (`imp config set --local <key> <value>`) —
  settings are never read from the quest's worktree.
  The old cherry-pick-onto-branch recipe only works when there is *no* snapshot.
- **Skew deadlock:** the snapshot is taken once and never auto-refreshed (the
  `--refresh-workflow` re-snapshot is unbuilt — `executor.py` references it only
  as a hypothetical). A quest that edits its own `plan-writer` / `spec-writer`
  *and* adds an invariant runs the OLD writers while `review` enforces the NEW
  invariant, producing an infinite remove/restore loop; `max_loops` is the only
  backstop. Recognize it when a quest loops on "removing/restoring its own
  plan/workflow file"; unblock by editing the snapshot.

## Moving or renaming a checkout

A project's state directory is claimed by the checkout's directory **name** —
`~/.imp/projects/<slug>/`, with a `repo` marker file inside naming the checkout that owns it.
Rename or move the checkout and the slug changes: imp claims a **new, empty** state directory,
and the old one is orphaned — quests, worktrees and the archive all appear to have vanished.
imp does not detect this. Recover by hand: rename the state directory to the new slug, repoint
its marker, and let git re-link the worktrees.

```bash
cd /new/path/to/checkout
OLD=~/.imp/projects/old-name    # substitute: the orphaned dir, whose `repo` marker names the OLD path
NEW=~/.imp/projects/new-name    # substitute: the slug imp resolves to now — `imp config` prints it
[ "$(ls -A "$NEW" 2>/dev/null)" = repo ] && rm -rf "$NEW"   # the empty dir imp claimed when you ran it here
if [ -e "$NEW" ]; then echo "STOP: $NEW still holds state — sort it out by hand"; else mv "$OLD" "$NEW"; fi   # refuses rather than nesting if $NEW still holds state
pwd -P > "$NEW/repo"            # the marker must hold the checkout's resolved path
for wt in "$NEW"/worktrees/*/; do [ -d "$wt" ] && git worktree repair "$wt"; done
imp config                      # `project state:` must name the new slug
imp list                        # your quests are back
```

The stub-clear and existence guard matter: any imp command you ran in the renamed checkout — including the one
that made you notice the problem — already claimed a **new, empty** `<new-name>` directory
holding just a `repo` marker. A plain `mv` would move the old directory *inside* it, leaving
`imp config` printing the right path over the wrong contents; removing the marker-only stub and
guarding the move against a non-empty `$NEW` makes that outcome unrepresentable (`mv -T` does the
same but is GNU-only and fails on macOS). Hence the closing `imp list`: `imp config`
alone cannot tell you the recovery worked.

**Finish or abandon in-flight quests before moving a checkout.** The steps above fix git's admin
files, but a quest's recorded `project_root` (in `quests/<id>/quest.json`) and its worktree's
`.imp/quests/<id>` symlink are absolute paths that nothing rewrites, so a quest still running when
the checkout moved will resume against the old, now-missing location.

`$IMP_STATE_DIR` sidesteps the slug entirely: set it and imp uses that path verbatim, so a
checkout that moves often can pin its state directory once.

---

*Part of imp's self-knowledge corpus. Runtime entry point: load the `imp-knowledge` skill (`load_skill(name="imp-knowledge")`), the thin index that maps into this corpus.*
