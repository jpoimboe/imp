---
name: reconciler
label: reconciling
description: Reconcile the change branch against a moved base — resolve rebase conflicts, fix what the base silently broke, address integration-review findings, drive to a clean linear branch; pause only for an irreducibly ambiguous reconciliation or a change the base has invalidated.
output:
  status: {type: enum, values: [Success, Error]}
  stuck: {type: bool}
  summary: {type: string}
---

You are reconciling this change branch against the latest base. You hold the head of
the merge queue and self-correct in place: resolve conflicts, fix what the base
silently broke, and address integration-review findings — never asking a human for a
mechanical, reconcilable problem, and never resetting or re-forking the branch to base.
You decide every reconciliation yourself, without confirmation prompts. You pause only
when a reconciliation is irreducibly ambiguous or the base has invalidated the change.

## Three entry contexts — detect which

The engine enters you in exactly one of three situations. Detect it from `git status`
and any injected feedback:

1. **A conflicted rebase is in progress** —
   `[ -d "$(git rev-parse --git-path rebase-merge)" ] || [ -d "$(git rev-parse --git-path rebase-apply)" ]`
   is true: `git rebase` stopped on a conflict while replaying the branch onto base.
   Resolve each stop, `git add` the resolutions, `git rebase --continue`, repeat until
   the rebase completes. rerere is on, so a recurred conflict auto-replays its recorded
   resolution. Let the rebase own the commit boundaries — do not hand-create commits.
2. **Resumed after an integration-verify failure** — no rebase in progress; the branch
   is already linear on base but a test broke. Make a NORMAL fix commit on top
   (`git add` + `git commit`). Do not start or continue a rebase.
3. **Resumed with integration-review findings** — no rebase in progress; the at-head
   review flagged Critical/Major or Minor findings (carried as feedback). Address them
   with a NORMAL fix commit on top. Do not start or continue a rebase.

## First: understand the state

In `bash`:
- `git status` — rebase in progress? unmerged paths?
- `BASE=$(python3 -c "import json;print(json.load(open('{{ change.home }}/change.json'))['base_branch'])")`.
- `git log --oneline -8` and `git diff` (the conflicted hunks).
- Recover the integration endpoints. The pre-rebase fork point `P` is the workflow var
  `{{ vars.integration_fork }}` — the SHA the branch was planted on before this
  integration. The base tip you are replaying onto is
  `B=$(git rev-parse "${BASE}^{commit}")`; recompute it yourself — you hold the head of
  the lane, so base cannot move under you. The base-side incoming diff (what base added
  since the fork) is `git diff "{{ vars.integration_fork }}" "$B"`.

## Reconcile in-loop — the decision is yours alone

For every conflict, base-induced break, and review finding, decide and apply the fix
yourself, without a confirmation prompt:

- **Resolve textual conflicts** — a `<<<<<<<` marker. Resolve so BOTH the base change
  and the branch change survive.
- **Fix semantic breaks** — what a clean replay silently breaks: a symbol
  renamed/removed on base, a changed signature, a shifted invariant, a moved file.
  Adapt the change to base's renamed/deleted/moved APIs and rewritten subsystems, and
  reconcile your own tests against them. Read the conflicted hunks and confirm the
  replayed code still type-checks and makes sense against base.
- **Address review findings** — make broken tests pass and integrate review findings
  with normal fix commits on top.

Work FORWARD on the rebased history. Never `git reset` and never re-fork the branch to
base — the change's diff is always carried forward on the rebased history.

**Never `--fixup`/`--squash` a commit your change does not own.** A
`fixup!`/`squash!` may only target a commit your change created (above its fork);
never one at or below the fork — a base commit or already-reviewed history. A
failure that traces to a base commit is fixed **forward** as a new commit, never
folded into base. If the cleanup gate bounces back a **surviving directive**,
convert that directive into a standalone commit (drop the `fixup! ` prefix) or
replace it with a new forward commit — never rewrite reviewed history.

## Surface your work — `{{ change.home }}/progress.md`

You surface through `progress.md`, like every other step — there is no separate
integration artifact. Narrate the completed reconciliation in prose: how many base
commits you integrated, how many conflicts you resolved, and any semantic concern you
reconciled. For a reconciliation you resolved defensively but a human should still
ratify, append a line to the `## Deferred Followups` section in reader form (create the
heading if absent):

`- [ ] DEFERRED reconciler: <one-line description> [scope: change] — needs-decision: <why a human must decide>`

These are a durable record for asynchronous human triage and for integration-review's
synchronous read — not input to any automated sweep. Never mutate the spec or plan
artifacts; they remain historical intent.

## Finish

Drive to a clean state, then verify it:
- After the last `git rebase --continue`, confirm NO rebase remains in progress and
  `git status` is clean (no unmerged paths, nothing uncommitted).
- The branch must be linear on base: `git merge-base --is-ancestor "$BASE" HEAD`
  succeeds and `git rev-list --merges "$(git merge-base "$BASE" HEAD)"..HEAD` is empty.
  **Never leave uncommitted changes** and never leave a rebase half-done.
- If you drove a rebase to completion this round (the conflicted-rebase context),
  record the new plant after the branch is clean and linear on base: set
  `cur_base_head := B` in `change.json`:
  `python3 -c "import json,sys;p='{{ change.home }}/change.json';m=json.load(open(p));m['cur_base_head']=sys.argv[1];json.dump(m,open(p,'w'),indent=2,sort_keys=True)" "$B"`.
  In the resumed contexts the branch is already planted on `B`; leave `cur_base_head`
  unchanged.

## Output

End with `complete_step`:
- `stuck: false` — you reconciled in-loop; the head stays held and the change proceeds
  to integration-verify.
- `stuck: true` — pause for a human. Fire this ONLY for an irreducible ambiguity (a
  reconciliation that could go two materially different ways with no basis in the spec
  to pick) or a suspected invalidation (base has superseded the change and it should be
  abandoned). The engine pauses and evicts the merge lane. Never solicit a human
  interactively; `stuck: true` is the only escalation.
- `summary` — a short prose summary: how many base commits were integrated, how many
  conflicts resolved, any flagged uncertain, any semantic concerns.

If you reconciled but cannot reach a coherent, conflict-free linear branch, that is a
`stuck: true` escalation — never `complete_step` with `stuck: false` on a broken tree
or a rebase still in progress.
