---
name: autosquash-resolve
label: resolving fixups
description: Resolve a mid-flight fixup!-fold rebase conflict so the fold preserves the pre-autosquash tree, then drive the rebase to completion.
output:
  status: {type: enum, values: [Success, Error]}
  summary: {type: string}
---

A `git rebase -i --autosquash` is **in progress** in this worktree: autosquash was
folding a `fixup!`/`squash!`/`amend!` commit into its target and hit a conflict. Your job
is to resolve the conflict(s) and drive the rebase to completion. This is pure git — no
project build or test tooling is involved.

## The hard constraint: the fold is tree-preserving

A `fixup!` fold is **tree-preserving by definition** — folding a fix into its target must
not change the final content, only how the commits are organised. So there is a single
correct answer: when the rebase finishes, `git rev-parse HEAD^{tree}` MUST equal the
pre-autosquash tree. That tree is recorded for you:

    cat {{ change.home }}/state/autosquash-before-tree

A re-entry of `cleanup.sh` (triggered by the sentinel file) checks this byte-for-byte after you finish. You
cannot "win" by editing freely — only by reproducing that exact tree with a clean,
one-commit-per-unit history. Resolve every conflict toward the content the pre-autosquash
branch already had.

## Procedure

1. Orient: `git status` and `git diff` to see the current stop and the conflicted paths.
2. For each conflicted path, resolve the markers so the result matches what the
   pre-autosquash tree holds for that path. Read the authoritative content of any path
   directly from the pre-autosquash commit:

       git cat-file -p "$(cat {{ change.home }}/state/autosquash-before-head):<path>"

3. `git add` each resolved path.
4. Continue: `GIT_EDITOR=true git rebase --continue`.
5. The rebase may stop several times. **Repeat 1–4 until the rebase is fully complete** —
   `git status` must no longer report a rebase in progress.

## Do not

- **Do not** `git rebase --abort` and **do not** `git rebase --skip`. Aborting throws away
  the fold; skipping drops a commit. Both fail verification.
- **Do not** introduce new content or "improve" anything. The only acceptable end state is
  the pre-autosquash tree reproduced with the fixups folded.

## Before finishing

Self-check that you reproduced the oracle and left no rebase running:

    test ! -d "$(git rev-parse --git-path rebase-merge)" \
      && [ "$(git rev-parse HEAD^{tree})" = "$(cat {{ change.home }}/state/autosquash-before-tree)" ] \
      && echo OK

End with `complete_step`: `status: Success` with a one-line `summary` of how many stops
you resolved. Re-entry of `cleanup.sh` routes the next step purely from the git state,
so if you cannot reproduce the tree, say so honestly in `summary` and report `status:
Error` — never fabricate a clean result.
