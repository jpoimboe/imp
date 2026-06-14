#!/usr/bin/env bash
# stamp-trailers.sh — the ONLY place tag policy lives (workflow-owned). Run once
# at the top of the review phase, immediately after autosquash. Stamps every
# non-merge commit in the change range with the `Change: <slug>` trailer, then
# self-verifies the postcondition before exit 0.
#
# Re-entry after integration: on the FIRST review pass the range is linear
# (integrate-rebase has not run yet), so we stamp from the fork point. But
# finalize runs AFTER integrate-rebase and can route rework -> implement, which
# re-enters this step with a real merge commit now in range. A plain `git rebase
# $FORK` would flatten that merge, silently dropping the integrator's baked-in
# resolution. So when a merge is present we stamp only the linear tail ABOVE the
# most recent integration merge (mirroring autosquash's cheap path): the pre-merge
# commits keep their first-pass stamps, the merge is left byte-identical, and the
# post-merge rework gets stamped.
# `trailer.ifexists=doNothing` prevents duplicate trailer lines, but each
# rebase amend rewrites commit SHAs — stamp-trailers is content-idempotent, not SHA-idempotent.
set -euo pipefail
cd "${IMP_PROJECT_ROOT:?IMP_PROJECT_ROOT not set by engine}"
SLUG="${IMP_CHANGE_SLUG:?IMP_CHANGE_SLUG not set by engine}"   # empty SLUG → fail fast
BASE=$(python3 -c "import json;print(json.load(open('.imp/change/change.json'))['base_branch'])")
FORK=$(git merge-base "$BASE" HEAD)

# Empty-range guard: a change always has spec+plan commits; an empty range means
# base detection is wrong, not "nothing to stamp".
[ -n "$(git rev-list "$FORK"..HEAD)" ] || { echo "empty $FORK..HEAD — base detection wrong" >&2; exit 1; }

# Stamp from the most recent integration merge when one is in range — its tail is
# linear, so a plain rebase is safe and the merge (with its resolution) is left
# untouched; otherwise stamp from the fork point. The full-range self-verify below
# still covers the pre-merge commits a prior pass stamped.
MERGE=$(git rev-list --merges --max-count=1 "$FORK"..HEAD)
STAMP_BASE=${MERGE:-$FORK}

if ! git rebase "$STAMP_BASE" \
  --exec 'git -c trailer.ifexists=doNothing commit --amend --no-edit --trailer "Change: '"$SLUG"'"'; then
  git rebase --abort 2>/dev/null || true
  echo "stamp-trailers rebase failed; branch left intact for human resolution" >&2
  exit 1
fi

# Self-verify: every NON-MERGE commit in the range now carries the trailer (a merge
# is integration plumbing, intentionally left unstamped). Catches a no-op --exec, a
# git that didn't run the exec, or a pre-merge commit a prior pass failed to stamp.
while read -r h; do
  [ "$(git log -1 --format='%(trailers:key=Change,valueonly)' "$h")" = "$SLUG" ] \
    || { echo "stamp failed: $h missing Change: $SLUG" >&2; exit 1; }
done < <(git rev-list --no-merges "$FORK"..HEAD)

echo "ok: stamped Change: $SLUG on all commits in $STAMP_BASE..HEAD"
