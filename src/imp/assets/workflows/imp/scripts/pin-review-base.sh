#!/usr/bin/env bash
# Print (newline-free) the SHA that defines the review diff range:
#   fork -> git merge-base <base_branch> HEAD   (the change's fork point)
#   head -> git rev-parse HEAD                   (the current tip)
# The closed expr language cannot trim strings, so this emits a clean SHA with no
# trailing newline (printf %s); `set: {review_base: output.stdout}` then stores a
# SHA that interpolates cleanly into `git diff {{ vars.review_base }}..HEAD`.
set -euo pipefail
cd "${IMP_PROJECT_ROOT:?IMP_PROJECT_ROOT not set by engine}"
mode="${1:?usage: pin-review-base.sh <fork|head>}"
case "$mode" in
  fork)
    BASE=$(python3 -c "import json;print(json.load(open('.imp/change/change.json'))['base_branch'])")
    [ -n "$BASE" ] || { echo "empty base_branch in change.json" >&2; exit 1; }
    git rev-parse --verify "${BASE}^{commit}" >/dev/null 2>&1 \
      || { echo "base ref not found: $BASE" >&2; exit 1; }
    result=$(git merge-base "$BASE" HEAD)
    printf %s "$result"
    ;;
  head)
    result=$(git rev-parse HEAD)
    printf %s "$result"
    ;;
  *)
    echo "unknown mode: $mode (expected fork|head)" >&2
    exit 2
    ;;
esac
