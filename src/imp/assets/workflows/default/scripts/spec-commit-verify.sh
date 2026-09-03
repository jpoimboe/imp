#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
set -euo pipefail
cd "${IMP_PROJECT_ROOT:?IMP_PROJECT_ROOT not set by engine}"
QUEST_DIR="${IMP_QUEST_DIR:?IMP_QUEST_DIR not set by engine}"
ID="${IMP_QUEST_ID:?IMP_QUEST_ID not set by engine}"
OUT="${IMP_OUTPUT:?IMP_OUTPUT not set by engine}"
LIVE="${QUEST_DIR}/artifacts/spec/${ID}.md"

# Seed before any branch can exit: the engine reads $IMP_OUTPUT after EVERY exit, and an empty file would hard-pause the gate.
printf '{}\n' > "$OUT"

if [ ! -f "$LIVE" ]; then
  echo "spec artifact $LIVE not found on disk" >&2
  exit 1
fi

N=$(awk '
  /^## Plan outline/    { inside = 1; next }
  inside && /^## /      { inside = 0; next }
  inside && /^[0-9]+\./ { n++ }
  END                   { print n + 0 }
' "$LIVE")

if [ "$N" -eq 0 ]; then
  echo "no numbered plans found under '## Plan outline' in $LIVE" >&2
  exit 1
fi

echo "ok: spec artifact present at $LIVE · $N plans in outline"
printf '{"plan_total": %d}\n' "$N" > "$OUT"
