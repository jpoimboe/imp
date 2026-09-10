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
  as a hypothetical). A quest that edits its own `plan-writer` / `spec-finalizer`
  *and* adds an invariant runs the OLD writers while `plan-reviewer` /
  `spec-reviewer` enforce the NEW
  invariant, producing an infinite remove/restore loop; `max_loops` is the only
  backstop. Recognize it when a quest loops on "removing/restoring its own
  plan/workflow file"; unblock by editing the snapshot.

## Recovering a dead provider login

- A ChatGPT login (codex) and a Claude login refresh through **single-use**
  refresh tokens: every refresh rotates the pair and invalidates the old one.
  imp refreshes them itself before each spawn and writes the new pair back to
  the canonical file — `~/.codex/auth.json` / `~/.claude/.credentials.json`.
- **Never copy a credential file between machines.** The second machine's first
  refresh — imp's or the vendor CLI's — rotates the token server-side, and the
  first machine's copy is dead from that moment. Log in separately on each host.
- A dead credential pauses the quest as `reauth_needed`. Re-login on this host
  (`codex login` / `claude login`), then `imp resume <id>` — the quest resumes in
  place and nothing is lost.
- If the file is shared and another host still holds a live copy, copying that
  current file back also works — until the next refresh on either host rotates
  it again.
- **codex only:** with `OPENAI_API_KEY` exported, a dead ChatGPT credential falls
  through to API-key auth on the next spawn and the run continues, with a
  warning. That bills the OpenAI API, not your ChatGPT subscription.

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
checkout that moves often can pin its state directory once. A pinned directory carries no
`repo` marker, so imp cannot tell whose state it holds and will not refuse a run from a
foreign checkout — a scratch run that inherited the variable writes straight into your live
quests. Opt in to the ownership guard by writing the marker yourself:

```bash
cd /path/to/the/owning/checkout
DIR=~/.imp/projects/whatever   # substitute: the directory $IMP_STATE_DIR names
pwd -P > "$DIR/repo"           # imp now refuses any other checkout pointed here
```

## Archive version skew

An imp older than the archive on disk refuses only the reads that touch the floor
that moved, and each floor refuses whole commands rather than degrading part of
one. An ahead index floor refuses `imp list` and `imp search` outright — both
query the archive's sqlite index, and there is no partial listing. An ahead blob
floor refuses reads of an **archived** quest's own record — `imp show`, `imp log`,
`imp events` on an archived id — while `imp list` and `imp search` still succeed
in full, since neither opens a record. A live quest stays reachable either way:
its id resolves against live homes and its data sits in its own home, so
`imp show`, `imp log`, `imp events`, `imp edit`, `imp abandon` and `imp resume`
all work for it while the archive is unreadable. A refusal is one line on stderr,
exit 2, no output:

```
imp: archive data is at v13, newer than this imp (knows v12). A running imp keeps the code it started with, so this is usually a stale process, not a stale checkout. Fix: Ctrl-D and `imp resume` to restart this quest on the current install; if it persists, update imp. Agents: suggest this to the user.
```

`archive index is at v...` is the same refusal for the index's schema floor.

The usual cause is a stale process, not a stale checkout. A running imp keeps the
code it started with: a session daemon warms its module graph at startup, so
rewriting `src/imp/` under a live process cannot splice newer code into it, and
the floors that process knows stay frozen at launch. Update the checkout while a
quest runs, let current code then archive something — a dependency's merge, say —
and the long-running daemon refuses the archive that write just bumped. Remedy:
Ctrl-D to pause the quest, then `imp resume <id>` on the current install. A
genuinely older checkout is the other cause — an agent working in a quest worktree
whose branch predates the archive's last format change, where `pythonpath: [src]`
resolves `imp` to that older source; there, update the checkout.

Writes are deliberately unguarded. An older imp may still archive a quest into a
newer archive; that lowers both floors (the blob `data_version` marker and the
index's `PRAGMA user_version`), and the next current imp re-migrates the blobs and
rebuilds the index, so the record heals itself. Refusing the write would strand a
quest on bookkeeping after its merge had already landed.

**Constraint on future schema work: a new index column must be nullable.** An older
writer's `index.ingest` never supplies it, and that row stands until a rebuild; a
`NOT NULL` column would make the older writer's archive fail outright.

**Constraint on future migrations: a blob migration that changes an index-visible
value must also bump the index schema version.** Each read checks only the floor it
touches, so a blob rewrite that changed what an index query reports would otherwise
slip past the index-only gate on an archive this imp cannot read.

---

*Part of imp's self-knowledge corpus. Runtime entry point: load the `imp-knowledge` skill (`load_skill(name="imp-knowledge")`), the thin index that maps into this corpus.*
