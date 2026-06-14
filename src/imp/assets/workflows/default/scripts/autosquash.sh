#!/usr/bin/env bash
# Fold every `fixup!`/`squash!`/`amend!` commit into its target (one clean commit
# per unit) AND — when re-entered after autosquash-resolve — VERIFY the fold
# preserved the pre-autosquash tree, both in this one invocation. The branch is
# always LINEAR here (base integration rebases, never merges), so this is the plain
# fork-point autosquash; there is no merge to preserve. The clean / no-op fold is a
# pure deterministic step (no agent). A fold conflict is handed to autosquash-resolve
# (exit 2), which on finishing re-runs THIS script; the `autosquash-before-tree`
# sentinel then selects verify-mode. Other failures abort non-zero -> engine pauses.
#   0 -> clean fold / verified resolution -> continue
#   2 -> resolvable fold conflict / verify failed with attempts left -> autosquash-resolve
#   1 -> hard failure / verify bound exhausted (branch restored pristine) -> pause
set -uo pipefail
cd "${IMP_PROJECT_ROOT:?IMP_PROJECT_ROOT not set by engine}" || exit 1
BASE=$(python3 -c "import json;print(json.load(open('.imp/change/change.json'))['base_branch'])")
# Rebase onto the FORK POINT, not the base tip — fold only this change's own commits.
FORK=$(git merge-base "$BASE" HEAD)
STATE=".imp/change/state"

# ── VERIFY MODE ──────────────────────────────────────────────────────────────
# A before-tree sentinel means a fold conflict was handed off and autosquash-resolve
# has now run; this re-entry VERIFIES the agent's resolution instead of starting a
# new fold. A fixup! fold is tree-preserving, so the resolution has a checkable
# correct answer: HEAD^{tree} == the recorded tree, no leftover directive, no rebase
# in progress. This block owns its own 3-attempt bound and the terminal pristine
# restore. Runs under `set -uo` (no -e) so the conditional git probes don't abort.
if [ -f "$STATE/autosquash-before-tree" ]; then
  BEFORE_TREE=$(cat "$STATE/autosquash-before-tree" 2>/dev/null || true)
  BEFORE_HEAD=$(cat "$STATE/autosquash-before-head" 2>/dev/null || true)
  ATTEMPTS_FILE="$STATE/autosquash-resolve-attempts"
  MAX_ATTEMPTS=3

  _cleanup() {
    rm -f "$STATE/autosquash-before-tree" "$STATE/autosquash-before-head" "$ATTEMPTS_FILE"
  }

  # First objective failure (empty REASON => verified). Order matters: an in-progress
  # rebase makes the tree check meaningless, so report it first.
  REASON=""
  if [ -d "$(git rev-parse --git-path rebase-merge)" ] \
     || [ -d "$(git rev-parse --git-path rebase-apply)" ]; then
    REASON="a rebase is still in progress (the fold was not driven to completion)"
  elif git log --format='%s' "$FORK"..HEAD | grep -qE '^(fixup|squash|amend)!'; then
    REASON="a fixup!/squash!/amend! subject still remains in $FORK..HEAD"
  elif [ "$(git rev-parse 'HEAD^{tree}')" != "$BEFORE_TREE" ]; then
    REASON="HEAD tree does not match the pre-autosquash tree (the fold changed content)"
  fi

  if [ -z "$REASON" ]; then
    _cleanup
    echo "autosquash: resolution verified (tree preserved, no leftovers, no rebase in progress)"
    exit 0
  fi

  PREV=$(cat "$ATTEMPTS_FILE" 2>/dev/null || echo 0)
  COUNT=$((PREV + 1))
  echo "$COUNT" > "$ATTEMPTS_FILE"

  if [ "$COUNT" -ge "$MAX_ATTEMPTS" ]; then
    # Bound exhausted: restore the branch to its exact pre-autosquash commit
    # OURSELVES, then pause. Abort any in-progress rebase first.
    git rebase --abort 2>/dev/null || true
    if [ -n "$BEFORE_HEAD" ]; then git reset -q --hard "$BEFORE_HEAD" 2>/dev/null || true; fi
    _cleanup
    echo "autosquash-resolve exhausted $MAX_ATTEMPTS attempts: $REASON" >&2
    echo "Restored the branch to its pre-autosquash state. Squash by hand, then resume:" >&2
    echo "  git rebase -i --autosquash \"\$(git merge-base $BASE HEAD)\"" >&2
    echo "resolve each conflict so the fold preserves the tree, then \`imp resume\`." >&2
    exit 1
  fi

  echo "autosquash: $REASON (attempt $COUNT/$MAX_ATTEMPTS); re-resolving" >&2
  exit 2
fi

# ── FOLD MODE ────────────────────────────────────────────────────────────────
# The deterministic fold runs under `set -e` (it relies on -e to abort on an
# unexpected git failure mid-rebase).
set -e

# Fast path: nothing to fold. Match every directive `git rebase --autosquash`
# reorders (fixup!/squash!/amend!).
if ! git log --format='%s' "$FORK"..HEAD | grep -qE '^(fixup|squash|amend)!'; then
  echo "no fixup!/squash!/amend! commits to autosquash"
  exit 0
fi

# Ordinary autosquash from the fork point. The branch is linear, so plain
# `rebase -i --autosquash $FORK` folds every directive with no merge to preserve.
# Capture the pre-autosquash HEAD + tree as the verify-mode oracle and the
# pristine-reset target; persist them only when we actually hand off a conflict.
BEFORE=$(git rev-parse HEAD)
BEFORE_TREE=$(git rev-parse "HEAD^{tree}")
# GIT_SEQUENCE_EDITOR=true accepts the reordered --autosquash todo list; GIT_EDITOR=true
# accepts the concatenated message a squash! fold opens the editor for (else
# editor-less envs wedge).
if GIT_SEQUENCE_EDITOR=true GIT_EDITOR=true git rebase -i --autosquash "$FORK"; then
  if git log --format='%s' "$FORK"..HEAD | grep -qE '^(fixup|squash|amend)!'; then
    echo "fixup! commits remain after autosquash (unmatched target subject?)" >&2
    exit 1
  fi
  echo "autosquash: folded fixups into their targets"
else
  # Distinguish a genuine fold CONFLICT (agent-resolvable: a rebase in progress AND
  # unmerged paths) from any other hard failure.
  if [ -d "$(git rev-parse --git-path rebase-merge)" ] && [ -n "$(git ls-files -u)" ]; then
    # Fold conflict: hand off. Do NOT --abort; leave the rebase in progress with the
    # conflict markers for autosquash-resolve.
    mkdir -p .imp/change/state
    rm -f .imp/change/state/autosquash-resolve-attempts
    printf '%s\n' "$BEFORE_TREE" > .imp/change/state/autosquash-before-tree
    printf '%s\n' "$BEFORE" > .imp/change/state/autosquash-before-head
    echo "autosquash: fixup fold conflicted; handing to autosquash-resolve (rebase left in progress)" >&2
    exit 2
  fi
  # Hard failure: best-effort restore to pristine, then pause.
  git rebase --abort 2>/dev/null || true
  echo "autosquash rebase failed with no resolvable fold conflict; branch left intact for human resolution" >&2
  exit 1
fi
