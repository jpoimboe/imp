#!/usr/bin/env bash
set -euo pipefail
cd "${IMP_PROJECT_ROOT:?IMP_PROJECT_ROOT not set by engine}"
CHANGE_DIR="${IMP_CHANGE_DIR:?IMP_CHANGE_DIR not set by engine}"
ID="${IMP_CHANGE_ID:?IMP_CHANGE_ID not set by engine}"
WIP="refs/imp/journal-wip/${ID}"
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
LIVE="${PLANS}/${NEWEST}"

if ! git rev-parse --verify --quiet "$WIP"; then
  echo "wip ref $WIP does not exist (plan not committed via artifact-commit.sh?)" >&2
  exit 1
fi
committed=$(git rev-parse --verify --quiet "${WIP}:plans/${NEWEST}") || {
  echo "$WIP tip does not carry plans/${NEWEST}" >&2; exit 1; }
live=$(git hash-object "$LIVE") || { echo "live plan $LIVE missing" >&2; exit 1; }
if [ "$committed" != "$live" ]; then
  echo "live plan $LIVE differs from the committed blob on $WIP" >&2
  exit 1
fi
echo "ok: $WIP carries plans/${NEWEST} matching $LIVE"
