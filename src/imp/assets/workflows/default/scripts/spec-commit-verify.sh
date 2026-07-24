#!/usr/bin/env bash
set -euo pipefail
cd "${IMP_PROJECT_ROOT:?IMP_PROJECT_ROOT not set by engine}"
QUEST_DIR="${IMP_QUEST_DIR:?IMP_QUEST_DIR not set by engine}"
ID="${IMP_QUEST_ID:?IMP_QUEST_ID not set by engine}"
LIVE="${QUEST_DIR}/artifacts/spec/${ID}.md"

if [ ! -f "$LIVE" ]; then
  echo "spec artifact $LIVE not found on disk" >&2
  exit 1
fi
echo "ok: spec artifact present at $LIVE"
