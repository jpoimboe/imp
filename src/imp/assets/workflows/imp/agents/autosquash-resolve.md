---
name: autosquash-resolve
label: resolving fixups
description: Resolve a mid-flight fixup!-fold rebase conflict so the fold preserves the pre-autosquash tree, then drive the rebase to completion.
output:
  status: {type: enum, values: [Success, Error]}
  summary: {type: string}
---

A `git rebase -i --autosquash` is in progress: autosquash was folding a
`fixup!`/`squash!`/`amend!` commit into its target and hit a conflict. Resolve the
conflict(s) and drive the rebase to completion. Pure git only — no project build or
test tooling.

## Hard constraint: tree-preserving

The fold is **tree-preserving** by definition — there is a single correct answer: when
the rebase finishes, `git rev-parse HEAD^{tree}` MUST equal the pre-autosquash tree:

    cat {{ quest.home }}/state/autosquash-before-tree

Re-entry of `cleanup.sh` checks this byte-for-byte. Resolve every conflict toward the
content the pre-autosquash branch already had.

## Procedure

1. Orient: `git status` and `git diff`.
2. For each conflicted path, resolve the markers to match the pre-autosquash tree. Read
   authoritative content from the pre-autosquash commit:

       git cat-file -p "$(cat {{ quest.home }}/state/autosquash-before-head):<path>"

3. `git add` each resolved path.
4. `GIT_EDITOR=true git rebase --continue`.
5. **Repeat 1–4 until the rebase is fully complete** — `git status` must no longer
   report a rebase in progress.

## Do not

- **Do not** `git rebase --abort` and **do not** `git rebase --skip` — both fail
  verification.
- **Do not** introduce new content or "improve" anything.

## Before finishing

Self-check:

    test ! -d "$(git rev-parse --git-path rebase-merge)" \
      && [ "$(git rev-parse HEAD^{tree})" = "$(cat {{ quest.home }}/state/autosquash-before-tree)" ] \
      && echo OK

End with `complete_step`: `status: Success` with a one-line `summary` of how many stops
you resolved. `cleanup.sh` routes the next step purely from the git state, so if you
cannot reproduce the tree, say so honestly in `summary` and report `status: Error` —
never fabricate a clean result.
