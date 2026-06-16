#!/usr/bin/env bash
# B3 cheap fast-path: compare the current base tip to the SHA integrate-rebase
# recorded. The common case on a shared integration branch is base advancing
# during the (arbitrarily long) human review gate. Catch it BEFORE taking the
# repo lock in merge-to-base.
#   0   -> base unchanged (or never integrated this run): continue into merge-to-base
#   10  -> base advanced since integration: re-integrate (engine rule: goto integrate-rebase)
#   1   -> preamble failure: pause for a human (do NOT misclassify as "advanced")
set -uo pipefail
# Preamble guards (review finding #4): a failing cd would otherwise continue in
# the engine's cwd (the workflow dir — a DIFFERENT repo under imp-on-imp) and an
# unguarded BASE read would flow into the compare and misclassify as exit 10.
cd "${IMP_PROJECT_ROOT:?IMP_PROJECT_ROOT not set by engine}" || { echo "base-fresh-check: cd to project root failed" >&2; exit 1; }
BASE=$(python3 -c "import json;print(json.load(open('.imp/change/change.json'))['base_branch'])") || { echo "base-fresh-check: failed to read base_branch from change.json" >&2; exit 1; }
[ -n "$BASE" ] || { echo "base-fresh-check: base_branch is empty" >&2; exit 1; }
CURRENT=$(git rev-parse "${BASE}^{commit}" 2>/dev/null) || { echo "base-fresh-check: base ref '${BASE}' is unresolvable" >&2; exit 1; }
RECORDED=$(cat .imp/change/integrated_base_sha 2>/dev/null || echo "")
# Missing RECORDED => integration never ran this run (e.g. a forced rewind to the
# merge phase, finding #7). Treat as "proceed to merge directly" (exit 0), NOT
# "advanced": the operator who rewound to merge wants to merge, not loop back into
# integration. In the normal flow integrate-rebase always records the SHA first, so
# missing-RECORDED only arises on such a rewind.
if [ -z "$RECORDED" ] || [ "$RECORDED" = "$CURRENT" ]; then
  echo "base unchanged or not integrated this run ($CURRENT); proceeding to merge"
  exit 0
fi
echo "base advanced ($RECORDED -> $CURRENT); re-integrating" >&2
exit 10
