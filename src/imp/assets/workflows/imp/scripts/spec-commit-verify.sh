#!/usr/bin/env bash
set -euo pipefail
cd "${IMP_PROJECT_ROOT:?IMP_PROJECT_ROOT not set by engine}"
CHANGE_DIR="${IMP_CHANGE_DIR:?IMP_CHANGE_DIR not set by engine}"
ID="${IMP_CHANGE_ID:?IMP_CHANGE_ID not set by engine}"
LIVE="${CHANGE_DIR}/artifacts/spec/${ID}.md"

if [ ! -f "$LIVE" ]; then
  echo "spec artifact $LIVE not found on disk" >&2
  exit 1
fi
echo "ok: spec artifact present at $LIVE"
