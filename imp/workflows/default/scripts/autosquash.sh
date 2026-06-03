#!/usr/bin/env bash
# Fold every `fixup!` commit into its target so the merged branch is one clean
# commit per unit (Resolution 6). Runs once at review-phase entry. No-op when
# there are no fixups. A rebase CONFLICT can't be resolved by an agent, so on
# conflict we abort and exit non-zero → the engine pauses for a human.
set -euo pipefail
cd "${IMP_PROJECT_ROOT:?IMP_PROJECT_ROOT not set by engine}"
BASE=$(python3 -c "import json;print(json.load(open('.imp/change/change.json'))['base_branch'])")
# Rebase onto the FORK POINT, not the base tip — so we only reorder/squash this
# change's own commits and never pull in base commits that landed during the change.
FORK=$(git merge-base "$BASE" HEAD)

# Fast path: nothing to fold.
if ! git log --format='%s' "$FORK"..HEAD | grep -q '^fixup!'; then
  echo "no fixup! commits to autosquash"
  exit 0
fi

# Non-interactive autosquash: GIT_SEQUENCE_EDITOR=true accepts the todo list that
# --autosquash has already reordered (fixups placed under their targets).
if GIT_SEQUENCE_EDITOR=true git rebase -i --autosquash "$FORK"; then
  if git log --format='%s' "$FORK"..HEAD | grep -q '^fixup!'; then
    echo "fixup! commits remain after autosquash (unmatched target subject?)" >&2
    exit 1
  fi
  echo "autosquash: folded fixups into their targets"
else
  git rebase --abort 2>/dev/null || true
  echo "autosquash rebase conflicted; branch left intact for human resolution" >&2
  exit 1
fi
