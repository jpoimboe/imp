#!/usr/bin/env bash
# Print (newline-free) the SHA that defines the review diff range:
#   fork -> change.json cur_base_head             (the change's recorded fork point)
#   head -> git rev-parse HEAD                     (the current tip)
# The closed expr language cannot trim strings, so this emits a clean SHA with no
# trailing newline (printf %s); `set: {review_base: output.stdout}` then stores a
# SHA that interpolates cleanly into `git diff {{ vars.review_base }}..HEAD`.
set -euo pipefail
cd "${IMP_PROJECT_ROOT:?IMP_PROJECT_ROOT not set by engine}"
mode="${1:?usage: pin-review-base.sh <fork|head>}"
case "$mode" in
  fork)
    : "${IMP_CHANGE_DIR:?IMP_CHANGE_DIR not set by engine}"
    result=$(python3 -c "import json,os;print(json.load(open(os.path.join(os.environ['IMP_CHANGE_DIR'],'change.json')))['cur_base_head'])")
    [ -n "$result" ] || { echo "empty cur_base_head in change.json" >&2; exit 1; }
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
