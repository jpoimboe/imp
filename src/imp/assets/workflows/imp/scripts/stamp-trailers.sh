#!/usr/bin/env bash
# stamp-trailers.sh — the ONLY place tag policy lives (workflow-owned). Run once
# at the top of the review phase, immediately after autosquash. Stamps every
# non-merge commit in the change range with the `Change: <id>` trailer, then
# self-verifies the postcondition before exit 0.
# `trailer.ifexists=doNothing` prevents duplicate trailer lines, but each
# rebase amend rewrites commit SHAs — stamp-trailers is content-idempotent, not SHA-idempotent.
set -euo pipefail
cd "${IMP_PROJECT_ROOT:?IMP_PROJECT_ROOT not set by engine}"
ID="${IMP_CHANGE_ID:?IMP_CHANGE_ID not set by engine}"   # empty ID → fail fast
: "${IMP_CHANGE_DIR:?IMP_CHANGE_DIR not set by engine}"
BASE=$(python3 -c "import json,os;print(json.load(open(os.path.join(os.environ['IMP_CHANGE_DIR'],'change.json')))['base_branch'])")
FORK=$(git merge-base "$BASE" HEAD)

# Empty-range guard: a change always has spec+plan commits; an empty range means
# base detection is wrong, not "nothing to stamp".
[ -n "$(git rev-list "$FORK"..HEAD)" ] || { echo "empty $FORK..HEAD — base detection wrong" >&2; exit 1; }

STAMP_BASE=$FORK

if ! git rebase "$STAMP_BASE" \
  --exec 'git -c trailer.ifexists=doNothing commit --amend --no-edit --trailer "Change: '"$ID"'"'; then
  git rebase --abort 2>/dev/null || true
  echo "stamp-trailers rebase failed; branch left intact for human resolution" >&2
  exit 1
fi

# Self-verify: every NON-MERGE commit in the range now carries the trailer (a merge
# is integration plumbing, intentionally left unstamped). Catches a no-op --exec, a
# git that didn't run the exec, or a pre-merge commit a prior pass failed to stamp.
while read -r h; do
  [ "$(git log -1 --format='%(trailers:key=Change,valueonly)' "$h")" = "$ID" ] \
    || { echo "stamp failed: $h missing Change: $ID" >&2; exit 1; }
done < <(git rev-list --no-merges "$FORK"..HEAD)

echo "ok: stamped Change: $ID on all commits in $STAMP_BASE..HEAD"
