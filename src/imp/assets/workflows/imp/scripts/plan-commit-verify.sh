#!/usr/bin/env bash
set -euo pipefail
cd "${IMP_PROJECT_ROOT:?IMP_PROJECT_ROOT not set by engine}"
CHANGE_DIR="${IMP_CHANGE_DIR:?IMP_CHANGE_DIR not set by engine}"
ID="${IMP_CHANGE_ID:?IMP_CHANGE_ID not set by engine}"
PLANS="${CHANGE_DIR}/artifacts/plans"

NEWEST=""
best=-1
for f in "${PLANS}/${ID}-plan-"*.md; do
  [[ -e "$f" ]] || continue
  n=${f##*-plan-}; n=${n%.md}
  [[ "$n" =~ ^[0-9]+$ ]] || continue
  if (( n > best )); then best=$n; NEWEST=$(basename "$f"); fi
done
if [[ -z "$NEWEST" ]]; then
  echo "no ${PLANS}/${ID}-plan-N.md found" >&2
  exit 1
fi
echo "ok: plan artifact present at ${PLANS}/${NEWEST}"
