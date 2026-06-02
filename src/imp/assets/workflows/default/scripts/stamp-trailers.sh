#!/usr/bin/env bash
# stamp-trailers.sh — the ONLY place tag policy lives (workflow-owned). Run once
# at the top of the review phase, immediately after autosquash, over the final,
# fixup-free history. Stamps every commit in the change range with the
# `Change: <slug>` trailer, then self-verifies the postcondition before exit 0.
set -euo pipefail
cd "${IMP_PROJECT_ROOT:?IMP_PROJECT_ROOT not set by engine}"
SLUG="${IMP_CHANGE_SLUG:?IMP_CHANGE_SLUG not set by engine}"   # empty SLUG → fail fast
BASE=$(python3 -c "import json;print(json.load(open('.imp/change/change.json'))['base_branch'])")
FORK=$(git merge-base "$BASE" HEAD)

# Empty-range guard: a change always has spec+plan commits; an empty range means
# base detection is wrong, not "nothing to stamp".
[ -n "$(git rev-list "$FORK"..HEAD)" ] || { echo "empty $FORK..HEAD — base detection wrong" >&2; exit 1; }

# Linear-history assumption: `git rebase` without --rebase-merges would drop a
# merge commit. Post-autosquash history is linear here; assert no merges in range.
if [ -n "$(git rev-list --merges "$FORK"..HEAD)" ]; then
  echo "merge commit in $FORK..HEAD — stamp-trailers assumes linear history" >&2
  exit 1
fi

if ! git rebase "$FORK" \
  --exec 'git -c trailer.ifexists=doNothing commit --amend --no-edit --trailer "Change: '"$SLUG"'"'; then
  git rebase --abort 2>/dev/null || true
  echo "stamp-trailers rebase failed; branch left intact for human resolution" >&2
  exit 1
fi

# Self-verify: every commit in the range now carries the trailer (catches a
# no-op --exec / a git that didn't run the exec). Postcondition, not a gate.
while read -r h; do
  [ "$(git log -1 --format='%(trailers:key=Change,valueonly)' "$h")" = "$SLUG" ] \
    || { echo "stamp failed: $h missing Change: $SLUG" >&2; exit 1; }
done < <(git rev-list "$FORK"..HEAD)

echo "ok: stamped Change: $SLUG on all commits in $FORK..HEAD"
