---
name: autosquash-resolve
label: resolving fixups
description: Make the end-of-quest fixup! fold reproduce the pre-autosquash tree — resolving a conflicted rebase, or repairing a fold that silently changed content.
output:
  status: {type: enum, values: [Success, Error]}
  summary: {type: string}
---

`cleanup.sh` could not fold this quest's `fixup!`/`squash!`/`amend!` commits without
changing content. You are in one of two shapes — `git status` tells you which:

- **Conflicted fold** — a `git rebase -i --autosquash` is still in progress. Resolve
  the conflicts and drive it to completion.
- **Silent divergence** — no rebase is in progress; the fold finished cleanly, but the
  tree moved. A `fixup!` is mis-targeted: one of its hunks repairs a line a *later*
  commit introduced, so hoisting it above that commit applies the change twice.

Pure git only — no project build or test tooling.

{% include "partials/coding-stance.md" %}

{% include "partials/action-care.md" %}

## Hard constraint: tree-preserving

The fold is **tree-preserving** by definition — there is a single correct answer: when
the rebase finishes, `git rev-parse HEAD^{tree}` MUST equal the pre-autosquash tree:

    cat {{ quest.home }}/state/autosquash-before-tree

Re-entry of `cleanup.sh` checks this byte-for-byte. Resolve every conflict toward the
content the pre-autosquash branch already had.

## Procedure — conflicted fold

1. Orient: `git status` and `git diff`.
2. For each conflicted path, resolve the markers to match the pre-autosquash tree. Read
   authoritative content from the pre-autosquash commit:

       git cat-file -p "$(cat {{ quest.home }}/state/autosquash-before-head):<path>"

3. `git add` each resolved path.
4. `GIT_EDITOR=true git rebase --continue`.
5. **Repeat 1–4 until the rebase is fully complete** — `git status` must no longer
   report a rebase in progress.

## Procedure — silent divergence

1. See what moved:

       git diff "$(cat {{ quest.home }}/state/autosquash-before-tree)" 'HEAD^{tree}'

2. Reset to the pre-fold branch:

       git reset --hard "$(cat {{ quest.home }}/state/autosquash-before-head)"

3. Name the culprit: with FORK=$(cat {{ quest.home }}/state/autosquash-fork),
   `git log -S'<divergent line>' "$FORK"..HEAD` names both the `fixup!` and the commit
   that actually owns the line.
4. Re-target it — reword the `fixup!` subject to the owning commit, or split the
   offending hunk into its own `fixup! <owning subject>` commit.
5. Re-fold:

       GIT_SEQUENCE_EDITOR=true GIT_EDITOR=true git rebase -i --autosquash "$FORK"

6. Repeat until no `fixup!` remains and `HEAD^{tree}` matches the pre-autosquash tree.

## Do not

- **Do not** `git rebase --abort` and **do not** `git rebase --skip` a rebase in
  progress — both fail verification.
- **Do not** paper over a divergence with a corrective commit at the tip — fix the
  commit that is wrong.
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
