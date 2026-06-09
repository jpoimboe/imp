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

# A prior base integration leaves a real merge commit in FORK..HEAD. Plain
# `rebase -i --autosquash $FORK` would linearize it, silently dropping the
# integrator's in-merge conflict resolutions (review finding #3). When a merge is
# in range, rebase only the segment ABOVE the most recent integration merge: a
# fixup! that targets a POST-merge commit folds into it while the merge commit
# (and the resolutions baked into its tree) is left untouched as the rebase base.
#
# A fixup! that targets a PRE-merge commit can't be folded above the merge, so it
# survives the rebase as a leftover; we detect that, restore the branch to its
# exact pre-run state, and refuse (the engine pauses). Auto-handling that hard
# case would require recreating the merge (`git rebase --rebase-merges` re-runs
# the merge and can drop the integrator's manual resolution), so it is left for a
# human / a future follow-up.
MERGE=$(git rev-list --merges --max-count=1 "$FORK"..HEAD)
if [ -n "$MERGE" ]; then
  BEFORE=$(git rev-parse HEAD)
  if GIT_SEQUENCE_EDITOR=true git rebase -i --autosquash "$MERGE"; then
    if git log --format='%s' "$FORK"..HEAD | grep -qE '^(fixup|squash|amend)!'; then
      git reset --hard "$BEFORE" >/dev/null
      echo "autosquash: a fixup!/squash!/amend! targets a commit at or below the integration" >&2
      echo "merge; folding it would linearize the merge and drop the integrator's resolutions." >&2
      echo "Refusing (branch left intact). Squash it by hand, preserving the merge:" >&2
      echo "  git rebase -i --rebase-merges --autosquash \"\$(git merge-base $BASE HEAD)\"" >&2
      echo "then \`imp resume\`.  [follow-up: auto-handle pre-merge-target fixups]" >&2
      exit 1
    fi
    echo "autosquash: folded post-merge fixups; integration merge preserved"
    exit 0
  fi
  git rebase --abort 2>/dev/null || true
  echo "autosquash rebase conflicted; branch left intact for human resolution" >&2
  exit 1
fi

# No integration merge in range: ordinary autosquash from the fork point.
# GIT_SEQUENCE_EDITOR=true accepts the todo list --autosquash has already reordered
# (fixups placed under their targets).
if GIT_SEQUENCE_EDITOR=true git rebase -i --autosquash "$FORK"; then
  if git log --format='%s' "$FORK"..HEAD | grep -qE '^(fixup|squash|amend)!'; then
    echo "fixup! commits remain after autosquash (unmatched target subject?)" >&2
    exit 1
  fi
  echo "autosquash: folded fixups into their targets"
else
  git rebase --abort 2>/dev/null || true
  echo "autosquash rebase conflicted; branch left intact for human resolution" >&2
  exit 1
fi
