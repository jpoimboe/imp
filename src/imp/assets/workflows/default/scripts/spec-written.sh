#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
# spec-written.sh — express-path gate after scoping: the threshold draft spec must
# be on disk before planning. Present -> print the marker (shown as the step's
# output), exit 0; missing -> recovery on stderr, exit 1 (the engine feeds it back
# to the resumed threshold turn).
set -euo pipefail
cd "${IMP_PROJECT_ROOT:?IMP_PROJECT_ROOT not set by engine}"
QUEST_DIR="${IMP_QUEST_DIR:?IMP_QUEST_DIR not set by engine}"
ID="${IMP_QUEST_ID:?IMP_QUEST_ID not set by engine}"
SPEC="${QUEST_DIR}/artifacts/spec/${ID}.md"

if [ ! -f "$SPEC" ]; then
  echo "No spec at $SPEC — write the draft spec there, then finish the step again." >&2
  exit 1
fi

echo "✓ spec written · imp show $ID spec"
