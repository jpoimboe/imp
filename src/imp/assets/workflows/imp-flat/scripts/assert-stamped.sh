#!/usr/bin/env bash
# assert-stamped.sh — read-only PRE-MERGE backstop. It performs NO rebase and NO
# mutation. Run immediately before merge-to-base, it asserts the invariant that
# cleanup already guarantees on every normal path: every non-merge commit in
# fork..HEAD carries `Change: <id>` and no fixup!/squash!/amend! directive subject
# remains. It almost never fires by design — it is defense against a FUTURE path
# that reaches merge-to-base without passing through cleanup, replacing the safety
# value of the deleted stamp-trailers self-check.
#   0 -> invariant holds -> merge
#   1 -> an unstamped commit or a leftover fixup directive -> pause for a human
set -euo pipefail
cd "${IMP_PROJECT_ROOT:?IMP_PROJECT_ROOT not set by engine}" || exit 1
ID="${IMP_CHANGE_ID:?IMP_CHANGE_ID not set by engine}"
: "${IMP_CHANGE_DIR:?IMP_CHANGE_DIR not set by engine}"
FORK=$(python3 -c "import json,os;print(json.load(open(os.path.join(os.environ['IMP_CHANGE_DIR'],'change.json')))['cur_base_head'])")
[ -n "$FORK" ] || { echo "empty cur_base_head in change.json" >&2; exit 1; }

# A here-string carries no pipe partner, so pipefail/SIGPIPE cannot make this spuriously
# fail; grep returns 1 (no directive => good) or 0 (a directive remains => fail).
if grep -qE '^(fixup|squash|amend)!' <<< "$(git log --format='%s' "$FORK"..HEAD)"; then
  echo "pre-merge assertion failed: a fixup!/squash!/amend! subject remains in $FORK..HEAD" >&2
  exit 1
fi

while read -r h; do
  [ -n "$h" ] || continue
  [ "$(git log -1 --format='%(trailers:key=Change,valueonly)' "$h")" = "$ID" ] \
    || { echo "pre-merge assertion failed: $h is missing Change: $ID" >&2; exit 1; }
done < <(git rev-list --no-merges "$FORK"..HEAD)

echo "ok: every non-merge commit in $FORK..HEAD carries Change: $ID; no fixup directives remain"
