# imp operator notes

Runbook for operating a running imp quest.

## Unblocking a paused quest

- A running quest executes from a **workflow snapshot** in its quest home
  (`~/.imp/projects/<slug>/quests/<id>/workflow/`, which the quest's worktree
  reaches as `.imp/quests/<id>/workflow/`) that shadows its branch (`quest.py` —
  `workflow_dir` returns the snapshot when present, else `live_workflow_dir`).
  Engine *code* fixes deploy on ff-merge (editable pipx install → `main`'s
  `src/`) to the *next* quest that starts: a **running** quest executes its own
  **source snapshot** of imp (see *Source snapshot* below) and picks up an engine
  fix only at its next `imp resume <id>`. Workflow *assets and scripts* never
  deploy on ff-merge at all — the workflow snapshot wins.
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
- A step that uses up its context handoffs pauses as `context handoff limit`.
  `imp resume <id>` continues it in a fresh session from its last handoff note;
  raise `context.max_reincarnations` if a step routinely needs more.

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

## Source snapshot

- A daemon copies the imp it is running — the `imp` and `impty` packages — into
  its quest home at startup, under the repo lock, and re-execs itself out of that
  copy: `~/.imp/projects/<slug>/quests/<id>/pinned-src/<token>/`, one directory
  per daemon start, never rewritten once published. The `imp` an agent runs
  inside the sandbox is that same **source snapshot**, read-only to the agent, so
  the CLI a sandboxed agent invokes and the process serving it over the session
  socket are always one build.
- **Consequence:** a merge to the base branch mid-run does not reach a running
  quest — by design, not by accident. Before this, a merge that changed a wire
  type mid-run left a freshly spawned in-sandbox `imp log` / `imp show` reading
  post-merge code while the process answering it was pre-merge, and it died with
  a raw `KeyError`. Two things outside the snapshot do still change under a
  running quest: imp's `docs/` corpus, which agents read live in place (a
  clone's `docs/`, a wheel install's packaged copy), and the project's local
  config layer above.
- `imp resume <id>` (after Ctrl-D) is how a running quest moves onto newer code:
  the resumed daemon takes a fresh source snapshot. Old snapshots need no
  tending — the next daemon started in that home deletes any whose owning process
  is gone, and reaping the quest home takes the rest.

## "agent's message may not have reached you"

Some models occasionally return a message as hidden thinking instead of visible text. When an agent stops to wait for you right after that happens, and its final reply is only a short line, imp assumes a question was lost: it shows this warning and asks the agent to send the message again before giving you the prompt. A false alarm costs one extra turn.

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
all work for it by its full id while the archive is unreadable. A refusal is one
line on stderr, exit 2, no output:

```
imp: archive data is at v18, newer than this imp (knows v17). A running imp keeps the code it started with, so this is usually a stale process, not a stale checkout. Fix: Ctrl-D and `imp resume` to restart this quest on the current install; if it persists, update imp. Agents: suggest this to the user.
```

`archive index is at v...` is the same refusal for the index's schema floor.

`imp resume <id>` is the one refusal that runs to more than one line. An id
matching any quest on the active board (`running`, `waiting`, `error`, `new`,
`paused`, `merge_blocked` or backlog) ignores archived matches, so version skew
cannot change the outcome. Any other id can match archived conversations, so
one matching an **archived** record this install cannot read refuses and names
that record. The floor line follows indented:

```
imp: 'lodo' matches an archived quest this imp cannot read: lodo77
  archive data is at v18, newer than this imp (knows v17). A running imp keeps the code it started with, so this is usually a stale process, not a stale checkout. Fix: Ctrl-D and `imp resume` to restart this quest on the current install; if it persists, update imp. Agents: suggest this to the user.
```

A quest with a home on disk always resolves by its full id, whatever the archive's
version. A finished quest's leftover home is not on the active board, so a partial
id matching it can still refuse over an unreadable archived record.

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

A run that has already merged no longer pauses when the stale process cannot file
it. The terminal status and the merge event are written before the archive is
touched, so a refusal there is post-terminal bookkeeping: imp keeps the
`completed` status, exits 0, and leaves the quest home standing under one warning —

```
warning: this quest finished but was not filed into the archive; imp files it on its next run
  archive data is at v18; this imp knows v17
```

The stranded-home sweep on the next `imp list` or `imp show` files the record, and
the daemon's startup reaper deletes the home after that — except below the blob
floor, where nothing files it and the stranding described below is permanent. Only
the home reap is deferred: the per-quest scratch dirs and the session socket are
still removed, since nothing else reaps them. Any other failure while archiving is
an engine bug and still pauses the run.

An older imp may still archive a quest into a newer archive; its trailing stamp
lowers both floors (the blob `data_version` marker and the index's `PRAGMA
user_version`). One blob migration ships in the tree, and it heals exactly one
step: a marker reading 16 is upgraded to 17 in place. `check_data_floor` runs
the upgrade on first blob-backed touch, and the heal at the top of
`ensure_current` runs it for the index-only readers (`imp list`, `imp search`),
so whichever comes first migrates the archive. The upgrade moves every quest
parked on `converge-verify`, the slow test step the default workflow retired, to
`wait-for-head` — in each resumable `archived` record and each live quest home —
clearing its armed resume and taking that step's labels, then rebuilds the index
and stamps both markers current, so a v16 archive is repaired rather than
refused. A quest moves only when its workflow, resolved the way its next run
will resolve it, has a `wait-for-head` step and no `converge-verify` step: one on
a committed fork of `default` that still ships the step, or whose workflow does
not resolve, is left alone. Rehydration restores state from the record, so no
blob is rewritten; completed and abandoned records keep the old step name as
history. The upgrade skips a live home whose lock is live, since its runner
would save over the rewrite. Three kinds of live home escape the upgrade: one it
skipped as live-locked, one a still-running pre-17 imp leaves at
`converge-verify` after it, and one in a project whose archive has no marker yet,
where the upgrade never runs. Each is healed by the stranded-home sweep on the
next `imp list`, `imp show` or `imp resume`, once its lock is released, and
before that sweep files any finished home. A pre-17 imp refuses to archive into
a v17 archive, so a home it finishes stays live until the sweep heals and files
it. The upgrade has to write, so a v16 archive on a read-only archive root is
refused rather than healed, where before this migration it read fine; make the
archive root writable and the next command heals it. Every other
below-floor version is a layout nothing in the tree can vouch for: a current imp
refuses that archive rather than stamping it current. Record and blob reads
raise, `ensure_current` leaves it exactly as it found it, nothing further is
archived into it, and the daemon's startup reaper (`reap_archived_homes`)
deletes no quest home — which can strand a quest on bookkeeping after its merge
has already landed, but leaves the homes that are its last locally readable copy
standing. Index-only queries (`imp list`, `imp search`) keep answering from the
stale index, and that index is never rebuilt: no code path raises a lowered
marker back to `_DATA_VERSION`, so for records written below any version but 16
the stranding is permanent. Recovery runs through the install that wrote a given
record — its floors match what it stamped, so it can still read it: check out
that commit to finish or extract the affected quest. Updating every imp that
writes here prevents further below-floor writes; it repairs none of the writes
already made.

**Constraint on future schema work: a new index column must be nullable.** An older
writer's `index.ingest` never supplies it, and that row stands until a rebuild; a
`NOT NULL` column would make the older writer's archive fail outright.

**Constraint on future migrations: a blob migration that changes an index-visible
value must also bump the index schema version.** Each read checks only the floor it
touches, so a blob rewrite that changed what an index query reports would otherwise
slip past the index-only gate on an archive this imp cannot read. The 16 → 17
migration is the worked example: the step it rewrites is the index's `step`
column, which the archived rows of `imp list` show, so `index._SCHEMA_VERSION`
went 9 → 10 alongside `_DATA_VERSION`, and the migration's own rebuild re-stamps
it in the same locked pass.

**Constraint on a further `_DATA_VERSION` bump: ship the migration and hang it
off both hooks — `check_data_floor` for blob-backed access, and the heal at the
top of `ensure_current` for index-only readers.** `check_data_floor` gates every
blob-backed read, write and delete: `archive_quest` calls it directly, and every
blob-backed path that reaches `ensure_current` clears it first
(`require_current`, or directly as `archive_quest` does). The index-only readers
in `archive/read.py` call `ensure_current` under `check_index_floor` alone, so
without the heal they would answer from the un-migrated index until some
blob-backed access happened: the heal runs the upgrade on a marker reading
exactly `_UPGRADABLE_FROM` before `ensure_current`'s data-version check, which
otherwise returns early on any mismatch. The bare-floor readers (`archived_ids`,
`is_archived`) call neither hook and read the index as it stands. The 16 → 17
upgrade is the shape to copy: version-exact (any other stored version keeps
raising), one-shot under the index filelock with a double-checked re-read, and a
no-op on a read-only archive root, which keeps today's refusal. A bump that also
adds an index column must ship that migration for the same reason: an archive
below the blob floor is never reindexed, `index.connect` only issues `CREATE
TABLE IF NOT EXISTS` and so leaves an older writer's column set alone, and a
query naming the new column would then fail outright instead of answering from
the stale index.

---

*Part of imp's self-knowledge corpus. Runtime entry point: load the `imp-knowledge` skill (`load_skill(name="imp-knowledge")`), the thin index that maps into this corpus.*
