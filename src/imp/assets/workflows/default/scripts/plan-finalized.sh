#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
set -euo pipefail
cd "${IMP_PROJECT_ROOT:?IMP_PROJECT_ROOT not set by engine}"
QUEST_DIR="${IMP_QUEST_DIR:?IMP_QUEST_DIR not set by engine}"
ID="${IMP_QUEST_ID:?IMP_QUEST_ID not set by engine}"
OUT="${IMP_OUTPUT:?IMP_OUTPUT not set by engine}"
PLAN_INDEX="${1:?plan_index arg missing}"
PLAN_TOTAL="${2:?plan_total arg missing}"
PLANS="${QUEST_DIR}/artifacts/plans"

NEWEST=""
best=-1
for f in "${PLANS}/${ID}-plan-"*.md; do
  [[ -e "$f" ]] || continue
  n=${f##*-plan-}; n=${n%.md}
  [[ "$n" =~ ^[0-9]+$ ]] || continue
  if (( n > best )); then best=$n; NEWEST=$f; fi
done
if [[ -z "$NEWEST" ]]; then
  echo "no ${PLANS}/${ID}-plan-N.md found" >&2
  exit 1
fi

count=$(grep -cE '^## Task [0-9]' "$NEWEST" || true)

noun=tasks
[[ "$count" == 1 ]] && noun=task
printf '✓ plan %s/%s · %s %s · imp show %s plans/%s\n' \
  "$PLAN_INDEX" "$PLAN_TOTAL" "$count" "$noun" "$ID" "${NEWEST##*/}"
n=0
while IFS= read -r line; do
  n=$((n + 1))
  title=${line#*: }
  printf '   %d. %s\n' "$n" "$title"
done < <(grep -E '^## Task [0-9]' "$NEWEST")

printf '{"task_total": %d}\n' "$count" > "$OUT"
