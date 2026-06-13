#!/usr/bin/env bash
# Objective gate after autosquash-resolve. A fixup! fold is tree-preserving, so the
# agent's resolution has a checkable correct answer: HEAD^{tree} must equal the
# pre-autosquash tree, with no leftover directive and no rebase in progress.
#
# This script — NOT the engine's max_loops — owns the retry bound and the terminal
# pristine restore. max_loops on a resume-loop pauses WITHOUT running any cleanup, which
# would leave a non-pristine mid-rebase tree. So we keep our own attempt counter and, on
# the final allowed attempt, restore the branch to the pre-autosquash commit ourselves.
#
#   0 -> pass: tree preserved, no leftovers, no rebase -> continue (stamp-trailers)
#   2 -> fail, attempts remain -> resume autosquash-resolve with the reason
#   1 -> fail, bound exhausted -> we restored pristine -> pause (human last resort)
set -uo pipefail
cd "${IMP_PROJECT_ROOT:?IMP_PROJECT_ROOT not set by engine}" || exit 1
BASE=$(python3 -c "import json;print(json.load(open('.imp/change/change.json'))['base_branch'])")
FORK=$(git merge-base "$BASE" HEAD)

STATE=".imp/change/state"
BEFORE_TREE=$(cat "$STATE/autosquash-before-tree" 2>/dev/null || true)
BEFORE_HEAD=$(cat "$STATE/autosquash-before-head" 2>/dev/null || true)
ATTEMPTS_FILE="$STATE/autosquash-resolve-attempts"
MAX_ATTEMPTS=3

_cleanup() {
  rm -f "$STATE/autosquash-before-tree" "$STATE/autosquash-before-head" "$ATTEMPTS_FILE"
}

# Identify the first objective failure (empty REASON => the resolution verified).
# Order matters: an in-progress rebase makes the tree check meaningless, so report it first.
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
  echo "autosquash-verify: resolution verified (tree preserved, no leftovers, no rebase in progress)"
  exit 0
fi

PREV=$(cat "$ATTEMPTS_FILE" 2>/dev/null || echo 0)
COUNT=$((PREV + 1))
echo "$COUNT" > "$ATTEMPTS_FILE"

if [ "$COUNT" -ge "$MAX_ATTEMPTS" ]; then
  # Bound exhausted: restore the branch to its exact pre-autosquash commit OURSELVES.
  # Abort any in-progress rebase first, then hard-reset to the recorded HEAD — this
  # covers BOTH a left-mid-flight rebase and a completed-but-drifted one (where there
  # is nothing to abort). The human takes over a pristine branch.
  git rebase --abort 2>/dev/null || true
  if [ -n "$BEFORE_HEAD" ]; then git reset -q --hard "$BEFORE_HEAD" 2>/dev/null || true; fi
  _cleanup
  echo "autosquash-resolve exhausted $MAX_ATTEMPTS attempts: $REASON" >&2
  echo "Restored the branch to its pre-autosquash state. Squash by hand, then resume:" >&2
  echo "  git rebase -i --autosquash \"\$(git merge-base $BASE HEAD)\"" >&2
  echo "resolve each conflict so the fold preserves the tree, then \`imp resume\`." >&2
  echo "See docs/autosquash-after-integration-recovery.md." >&2
  exit 1
fi

echo "autosquash-verify: $REASON (attempt $COUNT/$MAX_ATTEMPTS); re-resolving" >&2
exit 2
